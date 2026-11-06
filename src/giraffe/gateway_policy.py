"""Bounded observations of explicitly declared LiteLLM admission policies."""
from __future__ import annotations

import asyncio
from bisect import bisect_left
from decimal import Decimal
import time

from .identities import identity_headers, litellm_key_fingerprint
from .models import CheckResult, RequestSpec


VERSION = 9
PROMPT = "Reply with exactly the word OK."


async def execute(run, target, client):
    settings = run.config.gateway_policy
    if settings is None:
        return
    start = time.monotonic()
    end = min(start + settings.duration_seconds, run.deadline)
    observation = {"version": VERSION, "settings": settings.model_dump(mode="json"),
        "start_ms": (start-run.start)*1000, "end_ms": (end-run.start)*1000,
        "complete": False, "stop_reason": None, "offered": {user.identity: 0 for user in settings.users}}
    observation["isolated_run"] = set(run.config.checks) == {"gateway_policy"} and len(run.config.targets) == 1
    run.observations[target.name]["gateway_policy"] = observation
    if not settings.supported:
        observation["stop_reason"] = "Selected gateway capability is unsupported."
        return
    run.plan_condition(target.name, "gateway_policy", {},
                       planned=sum(user.requests for user in settings.users))

    # The default selects LiteLLM's native precedence, including its
    # X-LiteLLM-API-Key override. Custom deployments declare their key header.
    try:
        fingerprints = [litellm_key_fingerprint(identity_headers(target, user.identity),
                                                auth_header=settings.custom_key_header)
                        for user in settings.users]
    except ValueError:
        fingerprints = []
    reason = None
    if not fingerprints or not all(fingerprints):
        reason = "Required policy identity credentials are unavailable."
    elif len(set(fingerprints)) != len(fingerprints):
        reason = "Distinct authenticated test credentials could not be established locally."
    if reason:
        observation.update(credential_issue=True, stop_reason=reason, end_ms=observation["start_ms"])
        run.mark_unfinished(target.name, ["gateway_policy"])
        run.finish_condition(target.name, "gateway_policy", reason=reason)
        return
    observation["identities_distinct"] = True
    del fingerprints

    async def user_loop(user):
        for sequence in range(user.requests):
            if time.monotonic() >= end or not run.allowed(target.name):
                return
            ready_ms = (time.monotonic()-run.start)*1000
            spec = RequestSpec(fixture_id="gateway-policy-v1", scenario="gateway_policy",
                check_ids=["gateway_policy"], identity=user.identity,
                messages=[{"role": "user", "content": PROMPT}], stream=False,
                max_tokens=settings.max_output_tokens,
                options={"temperature": 0, "cache": {"no-cache": True, "no-store": True}},
                workload={"user_id": user.identity, "request_sequence": sequence+1,
                          "ready_ms": ready_ms, "declared_priority": user.priority})

            def dispatched():
                observation["offered"][user.identity] += 1

            record = await run.request(target, client, spec, admission_deadline=end,
                                       on_start=dispatched)
            if record is None:
                return
            if sequence+1 < user.requests:
                await asyncio.sleep(min(settings.interval_seconds, max(0, end-time.monotonic())))

    tasks = [asyncio.create_task(user_loop(user)) for user in settings.users]
    try:
        async with asyncio.timeout_at(end):
            await asyncio.gather(*tasks)
        observation["complete"] = all(observation["offered"][u.identity] == u.requests for u in settings.users)
    except TimeoutError:
        observation["stop_reason"] = "Policy traffic duration reached."
    except BaseException:
        observation["stop_reason"] = "Policy traffic interrupted."
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        observation["end_ms"] = min((time.monotonic()-run.start)*1000, observation["end_ms"])
        observation["stop_reason"] = observation["stop_reason"] or run.stop_reason(target.name)
        if not observation["complete"]:
            run.mark_unfinished(target.name, ["gateway_policy"])
        run.finish_condition(target.name, "gateway_policy", reason=observation["stop_reason"])


def _completed_before_dispatch(admissions, record):
    if record.dispatch_ms is None:
        return 0
    return sum(other is not record and other.completed_ms is not None and other.completed_ms < record.dispatch_ms
               for other in admissions)


def summarize(settings, observation, records):
    users = []
    for user in settings.users if settings else []:
        own = [record for record in records if record.identity == user.identity]
        completed = [record for record in own if record.status == "completed" and record.valid]
        users.append({"identity": user.identity, "declared_rpm": user.rpm, "priority": user.priority,
            "planned": user.requests, "dispatched": len(own), "completed": len(completed),
            "admitted": sum(record.http_status is not None and 200 <= record.http_status < 300 for record in own),
            "rejected": sum(record.http_status == 429 for record in own),
            "other_failures": sum((record.status in {"failed", "timeout"} or record.status == "completed" and not record.valid) and record.http_status != 429 for record in own),
            "cancelled": sum(record.status == "cancelled" for record in own),
            "input_tokens": [record.input_tokens for record in completed],
            "output_tokens": [record.output_tokens for record in completed],
            "completion_tokens": [record.completion_tokens for record in completed],
            "reasoning_tokens": [record.reasoning_tokens for record in completed],
            "request_ids": [record.id for record in own],
            "evidence": [{"request_id": record.id, "http_status": record.http_status,
                          "policy": record.gateway_evidence.model_dump(mode="json") if record.gateway_evidence else None}
                         for record in own]})
    summary = {"version": VERSION, "settings": settings.model_dump(mode="json") if settings else {}, "users": users,
        "start_ms": observation.get("start_ms"), "end_ms": observation.get("end_ms"),
        "complete": observation.get("complete", False), "stop_reason": observation.get("stop_reason"),
        "identities_distinct": observation.get("identities_distinct", False),
        "qualification": "unverified", "status": "inconclusive", "reasons": [],
        "peer_continuation": False, "overlap_observed": False, "comparable_actual_work": False,
        "response_cache_hit_reported": False,
        "interpretation": "Observed admission under declared policy and window assumptions; no queue-order, latency-causality or production-wide guarantee."}

    def outcome(qualification, reason, status="inconclusive"):
        summary.update(qualification=qualification, status=status)
        summary["reasons"].append(reason)
        return summary

    if settings is None:
        return outcome("unavailable", "Gateway policy is not configured; declare identities, policy and offered work.")
    if not settings.supported:
        return outcome("unavailable", "Selected gateway capability is unsupported.")
    if observation.get("credential_issue"):
        return outcome("unverified", observation["stop_reason"])
    if any(record.gateway_evidence and record.gateway_evidence._response_cache_hit_reported for record in records):
        summary["response_cache_hit_reported"] = True
        return outcome("unverified", "Gateway reported a cached completion, so this policy probe could not verify fresh work.")
    if settings.capability == "priority_rpm" and any(user.priority == "default_pool" for user in settings.users):
        return outcome("unverified", "LiteLLM reserves default_pool for unreserved traffic. Use distinct named reservations.")
    if not records:
        return outcome("unexercised", "No policy traffic was dispatched.")
    affected = [record for record in records if record.identity == settings.affected_identity]
    peers = [record for record in records if record.identity != settings.affected_identity]
    successes = [record for record in records if record.status == "completed" and record.valid]
    admissions = [record for record in records if record.http_status is not None and 200 <= record.http_status < 300]
    affected_admissions = [record for record in admissions if record.identity == settings.affected_identity]
    declared = next(user for user in settings.users if user.identity == settings.affected_identity)
    rejection_kind = "key_rpm" if settings.capability == "key_rpm" else "priority_pool"
    affected_rejections = [record for record in affected if record.http_status == 429]
    recognized = [record for record in affected_rejections if record.gateway_evidence
                  and record.gateway_evidence.policy_rejection == rejection_kind
                  and record.gateway_evidence.rate_limit_unit == "requests"]
    for rejection in recognized:
        summary["peer_continuation"] = all(any(record.identity == user.identity
            and record.status == "completed" and record.valid
            and record.dispatch_ms is not None and rejection.completed_ms is not None
            and record.dispatch_ms > rejection.completed_ms and record.completed_ms is not None
            and record.completed_ms <= observation.get("end_ms", 0)
            for record in peers) for user in settings.users if user.identity != settings.affected_identity)
        if summary["peer_continuation"]:
            break
    summary["overlap_observed"] = any(a.status == "completed" and a.valid and b.status == "completed" and b.valid
        and a.dispatch_ms is not None and a.completed_ms is not None
        and b.dispatch_ms is not None and b.completed_ms is not None
        and max(a.dispatch_ms, b.dispatch_ms) < min(a.completed_ms, b.completed_ms)
        for a in affected for b in peers)
    inputs = [record.input_tokens for record in successes]
    outputs = [record.completion_tokens for record in successes]
    summary["comparable_actual_work"] = bool(successes and all(row["completed"] for row in users)
        and all(value is not None and value > 0 for value in inputs+outputs)
        and len(set(inputs)) == 1 and max(outputs) <= 2*min(outputs))
    if settings.window_state == "unknown" or not settings.gateway_version or not observation.get("isolated_run"):
        return outcome("unverified", "Gateway version or isolated fresh single-target policy window is unknown.")
    if settings.window_state != "isolated_fresh_single_counter":
        return outcome("unverified", "Policy qualification requires an operator-confirmed authoritative limiter counter.")
    if not observation.get("identities_distinct"):
        return outcome("unverified", "Distinct authenticated test credentials could not be established locally.")
    if not recognized:
        if affected_rejections:
            return outcome("unverified", "Rejection does not identify the declared gateway policy.")
        return outcome("unexercised", "No attributed policy rejection was observed within offered work.")
    windows = [record.gateway_evidence.retry_after_seconds for record in recognized]
    start, end = observation.get("start_ms"), observation.get("end_ms")
    # LiteLLM v3 reports the configured window and truncates bucket start
    # timestamps to whole seconds. A fresh bucket can reset almost 1s early.
    if (any(window is None or window <= 1 for window in windows) or len(set(windows)) != 1
            or start is None or end is None or end < start or end - start >= (windows[0] - 1)*1000):
        return outcome("unverified", "Cannot establish a single policy window from Retry-After and the observation duration.")
    summary["effective_window_seconds"] = windows[0]
    allowance = declared.rpm
    if settings.capability == "priority_rpm":
        if settings.saturation_cache_seconds != 0:
            return outcome("unverified", "Cached saturation can differ between gateway nodes; this check requires zero cache delay.")
        evidence = [record.gateway_evidence for record in recognized]
        if any(item.model_rpm != settings.model_rpm or item.priority_matches is not True for item in evidence):
            return outcome("unverified", "Priority tier or model RPM evidence does not match the declaration.")
        if not summary["overlap_observed"] or not summary["comparable_actual_work"]:
            return outcome("unverified", "Comparable actual token work and overlapping requests are required for priority observations.")
        if any(record.gateway_evidence is None or record.gateway_evidence.priority_matches is not True for record in admissions):
            return outcome("unverified", "Successful responses do not confirm each declared priority tier.")
        groups = {record.gateway_evidence._priority_model_group if record.gateway_evidence else None
                  for record in [*recognized, *admissions]}
        if len(groups) != 1 or None in groups:
            return outcome("unverified", "A common resolved model pool is not established by the priority evidence.")
        reservations = {user.identity: user.rpm for user in settings.users}
        if any(record.gateway_evidence.limiter_version != "v3"
               or record.gateway_evidence.priority_request_limit != reservations[record.identity]
               for record in admissions):
            return outcome("unverified", "Scoped v3 priority RPM limits are missing or differ from the declared reservations.")
        # The last borrowed request crosses the same ratio threshold as LiteLLM.
        crossing = bisect_left(range(settings.model_rpm + 1), settings.saturation_threshold,
                               key=lambda count: count / settings.model_rpm)
        threshold_percent = Decimal(str(settings.saturation_threshold)) * 100
        for rejection in recognized:
            bounds = rejection.gateway_evidence._saturation_bounds
            completed = _completed_before_dispatch(admissions, rejection)
            if bounds is None or bounds[1] < threshold_percent or (
                bounds[0] < threshold_percent and completed < crossing
            ):
                return outcome("unverified", "Strict-mode evidence does not establish the declared saturation threshold.")
            if completed * 100 > bounds[1] * settings.model_rpm:
                return outcome("unverified", "Reported saturation contradicts completed admissions in the isolated window.")
        if len(admissions) > settings.model_rpm:
            return outcome("observed_failure", "Total admitted requests exceeded the declared model RPM in the isolated window.", "fail")
        allowance = max(allowance, crossing)
    elif any(record.gateway_evidence.request_limit != declared.rpm or record.gateway_evidence.request_remaining != 0 for record in recognized):
        return outcome("unverified", "Gateway key limit differs from the declared RPM.")
    admitted = len(affected_admissions)
    if admitted > allowance:
        return outcome("observed_failure", "Admitted requests exceeded the declared RPM allowance in the isolated window.", "fail")
    first_rejection = min((record.completed_ms for record in recognized if record.completed_ms is not None), default=None)
    if first_rejection is None:
        return outcome("unverified", "Attributed rejection timing is unavailable.")
    if settings.capability == "priority_rpm" and any(
        record.dispatch_ms is not None and record.dispatch_ms > first_rejection
        and record.completed_ms is not None and record.completed_ms <= observation.get("end_ms", 0)
        for record in affected_admissions
    ):
        return outcome("observed_failure", "A request dispatched after strict priority pool exhaustion was admitted in the same window.", "fail")
    borrowing_uncertain = False
    if settings.capability == "priority_rpm":
        rpm_only_saturation = all(item._model_tpm_unconfigured for item in evidence)
        # Each identity dispatches serially, so only admissions beyond its
        # reservation depend on whether the shared pool had reached saturation.
        for record in affected_admissions[declared.rpm:]:
            if _completed_before_dispatch(admissions, record) >= crossing:
                return outcome("observed_failure", "A request exceeded its reserved RPM after completed admissions reached model saturation.", "fail")
            if record.completed_ms is None or sum(
                other is not record and (other.dispatch_ms is None or other.dispatch_ms <= record.completed_ms)
                for other in admissions
            ) >= crossing:
                borrowing_uncertain = True
    prior_admissions = [record for record in affected_admissions
                        if record.completed_ms is not None and record.completed_ms < first_rejection]
    if len(prior_admissions) < declared.rpm:
        # RPM can be charged before a downstream failure; its admission is unknown.
        uncertain_prior = sum(record not in prior_admissions and record not in recognized
                              and (record.dispatch_ms is None or record.dispatch_ms < first_rejection)
                              for record in affected)
        if len(prior_admissions) + uncertain_prior >= declared.rpm:
            return outcome("unverified", "Prior requests with unknown admission could have consumed the declared RPM allowance.")
        return outcome("observed_failure", "Attributed rejection occurred before the declared RPM allowance was served.", "fail")
    if len(recognized) != len(affected_rejections):
        return outcome("unverified", "Affected identity received 429 responses that do not identify the declared gateway policy.")
    if not summary["complete"] or any(user["cancelled"] or user["other_failures"] for user in users):
        return outcome("unverified", "Traffic was interrupted or non-policy failures left incomplete evidence.")
    if settings.capability == "priority_rpm" and admitted > declared.rpm and not rpm_only_saturation:
        return outcome("unverified", "Model TPM is configured or unknown, so request counts cannot establish borrowing below saturation.")
    if borrowing_uncertain:
        return outcome("unverified", "Overlapping admissions leave saturation uncertain when a request exceeded its reserved RPM.")
    if not summary["peer_continuation"]:
        return outcome("unexercised", "Peer service after policy rejection was not observed within the same window.")
    if any(user["rejected"] for user in users if user["identity"] != settings.affected_identity):
        return outcome("unverified", "Peer rejections prevent establishing continued service within declared limits.")
    return outcome("observed", "Attributed policy rejection and continued peer service were observed.", "pass")


def result(run, target):
    observation = run.observations[target.name].get("gateway_policy", {})
    records = [record for record in run.records if record.target == target.name and "gateway_policy" in record.check_ids]
    summary = summarize(run.config.gateway_policy, observation, records)
    return CheckResult(id="gateway_policy", title="Gateway quotas & priorities", target=target.name,
        status=summary["status"], summary=" ".join(summary["reasons"]), metrics={"gateway_policy": summary},
        required=summary["qualification"] != "unavailable" or run.config.checks == ["gateway_policy"],
        evidence_ids=[record.id for record in records])
