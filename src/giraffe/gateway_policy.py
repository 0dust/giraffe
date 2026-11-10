"""Bounded observations of explicitly declared LiteLLM admission policies."""
from __future__ import annotations

import asyncio
from bisect import bisect_left, bisect_right
from decimal import Decimal
from fractions import Fraction
from math import ceil, floor, inf
import time

from .identities import gateway_identities_only, identity_headers, litellm_key_fingerprint
from .models import CheckResult, RequestSpec


VERSION = 23
PROMPT = "Reply with exactly the word OK."


def deferred_for_shared_targets(config):
    return (len(config.targets) > 1 and "gateway_policy" in config.checks
            and config.gateway_policy is not None and config.gateway_policy.supported
            and not gateway_identities_only(config.checks))


def credential_issue(settings, target):
    # The default selects LiteLLM's native precedence, including its
    # X-LiteLLM-API-Key override. Custom deployments declare their key header.
    try:
        fingerprints = [litellm_key_fingerprint(identity_headers(target, user.identity),
                                                auth_header=settings.custom_key_header)
                        for user in settings.users]
    except ValueError:
        fingerprints = []
    if not fingerprints or not all(fingerprints):
        return "Required policy identity credentials are unavailable."
    if len(set(fingerprints)) != len(fingerprints):
        return "Distinct authenticated test credentials could not be established locally."
    return None


async def execute(run, target, client):
    with run.inline_gateway_limit_phase():
        await _execute(run, target, client)


async def _execute(run, target, client):
    settings = run.config.gateway_policy
    if settings is None:
        return
    start = time.monotonic()
    end = min(start + settings.duration_seconds, run.deadline)
    observation = {"version": VERSION, "settings": settings.model_dump(mode="json"),
        "start_ms": (start-run.start)*1000, "end_ms": (end-run.start)*1000,
        "complete": False, "stop_reason": None, "offered": {user.identity: 0 for user in settings.users}}
    observation["isolated_run"] = (set(run.config.checks) == {"gateway_policy"}
                                   and len(run.config.targets) == 1 and run.config.restart_target is None)
    run.observations[target.name]["gateway_policy"] = observation
    if not settings.supported:
        observation["stop_reason"] = "Selected gateway capability is unsupported."
        return
    run.plan_condition(target.name, "gateway_policy", {},
                       planned=sum(user.requests for user in settings.users))

    reason = credential_issue(settings, target)
    if reason:
        observation.update(credential_issue=True, stop_reason=reason, end_ms=observation["start_ms"])
        run.finish_condition(target.name, "gateway_policy", reason=reason)
        return
    observation["identities_distinct"] = True
    if (not gateway_identities_only(run.config.checks) and run.remaining(target.name) == 0
            and run.stop_reason(target.name) in (None, "maximum request budget reached")):
        # A completed shared workload can legitimately use the entire quota.
        # An unsent optional probe does not interrupt that workload.
        run.mark_request_limit(gateway_probe=True)
        reason = "The request limit left no slots for the configured gateway policy probes."
        observation.update(budget_limited=True, stop_reason=reason, end_ms=observation["start_ms"])
        run.finish_condition(target.name, "gateway_policy", reason=reason)
        return

    async def user_loop(user):
        for sequence in range(user.requests):
            if time.monotonic() >= end or not run.allowed(target.name, gateway_probe=True):
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
                                       prepared_admission=True, on_start=dispatched)
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
        finished = time.monotonic()
        if finished >= end:
            observation["complete"] = False
            observation["stop_reason"] = observation["stop_reason"] or "Policy traffic duration reached."
        observation["end_ms"] = min((finished-run.start)*1000, observation["end_ms"])
        if not observation["complete"]:
            reason = observation["stop_reason"] or run.stop_reason(target.name)
            if reason in (None, "maximum request budget reached") and run.remaining(target.name) == 0:
                observation["budget_limited"] = True
                reason = reason or "The target request limit interrupted the configured gateway policy probes."
            observation["stop_reason"] = reason
            run.mark_unfinished(target.name, ["gateway_policy"])
        run.finish_condition(target.name, "gateway_policy", reason=observation["stop_reason"])


def _completed_before_dispatch(admissions, record, *, after_ms=None):
    if record.dispatch_ms is None:
        return 0
    return sum(other is not record and other.completed_ms is not None and other.completed_ms < record.dispatch_ms
               and (after_ms is None or other.dispatch_ms is not None and after_ms < other.dispatch_ms)
               for other in admissions)


def _least_reported_count(reported, limit, minimum, maximum):
    # Match LiteLLM's float percent formatting without scanning a whole TPM cap.
    candidates = range(minimum, maximum + 1)
    for printed in reported:
        precision = f".{-printed.as_tuple().exponent}%"

        def rounded(count):
            return Decimal(format(count / limit, precision)[:-1])

        first = bisect_left(candidates, printed, key=rounded)
        last = bisect_right(candidates, printed, lo=first, key=rounded)
        candidates = candidates[first:last]
        if not candidates:
            return None
    return candidates[0] if candidates else None


def _scoped_limit_conflicts(record, expected):
    evidence = record.gateway_evidence
    return evidence is not None and any(
        scope in expected and (value is None or value != expected[scope])
        for scope, value in evidence._scoped_limits.items()
    )


def _proven_admission(record):
    return (record.status == "completed" and record.valid
            and record.http_status is not None and 200 <= record.http_status < 300)


def summarize(settings, observation, records):
    users = []
    for user in settings.users if settings else []:
        own = [record for record in records if record.identity == user.identity]
        completed = [record for record in own if record.status == "completed" and record.valid]
        users.append({"identity": user.identity, "declared_rpm": user.rpm, "priority": user.priority,
            "planned": user.requests, "dispatched": len(own), "completed": len(completed),
            "admitted": sum(_proven_admission(record) for record in completed),
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
    if observation.get("execution_error"):
        return outcome("unverified", observation["execution_error"])
    if observation.get("credential_issue") or (observation.get("budget_limited") and not records):
        return outcome("unverified", observation["stop_reason"])
    cached = {record.id for record in records
              if record.gateway_evidence and record.gateway_evidence._response_cache_hit_reported}
    summary["response_cache_hit_reported"] = bool(cached)
    if settings.capability == "priority_rpm" and any(user.priority == "default_pool" for user in settings.users):
        return outcome("unverified", "LiteLLM reserves default_pool for unreserved traffic. Use distinct named reservations.")
    if not records:
        return outcome("unexercised", "No policy traffic was dispatched.")
    start, end = summary["start_ms"], summary["end_ms"]
    # Completion bounds when the gateway might have admitted a request. A
    # client dispatch alone cannot place that admission inside this window.
    window_records = [record for record in records if start is not None and end is not None
                      and record.dispatch_ms is not None and record.completed_ms is not None
                      and start <= record.dispatch_ms <= record.completed_ms <= end]
    incomplete_window = len(window_records) != len(records)
    if incomplete_window:
        summary["complete"] = False
    if any(record.id in cached for record in window_records):
        return outcome("unverified", "Gateway reported a cached completion, so this policy probe could not verify fresh work.")
    affected = [record for record in window_records if record.identity == settings.affected_identity]
    admissions = [record for record in window_records if _proven_admission(record)]
    affected_admissions = [record for record in admissions if record.identity == settings.affected_identity]
    peer_admissions = [record for record in admissions if record.identity != settings.affected_identity]
    declared = next(user for user in settings.users if user.identity == settings.affected_identity)
    rejection_kind = "key_rpm" if settings.capability == "key_rpm" else "priority_pool"
    affected_rejections = [record for record in affected if record.http_status == 429]
    recognized = [record for record in affected_rejections if record.gateway_evidence
                  and record.gateway_evidence.policy_rejection == rejection_kind
                  and record.gateway_evidence.rate_limit_unit == "requests"]
    for rejection in recognized:
        summary["peer_continuation"] = all(any(record.identity == user.identity
            and record.dispatch_ms > rejection.completed_ms
            for record in peer_admissions) for user in settings.users if user.identity != settings.affected_identity)
        if summary["peer_continuation"]:
            break
    summary["overlap_observed"] = any(max(a.dispatch_ms, b.dispatch_ms) < min(a.completed_ms, b.completed_ms)
        for a in affected_admissions for b in peer_admissions)
    inputs = [record.input_tokens for record in admissions]
    outputs = [record.completion_tokens for record in admissions]
    summary["comparable_actual_work"] = bool({record.identity for record in admissions} == {user.identity for user in settings.users}
        and all(value is not None and value > 0 for value in inputs+outputs)
        and all(record.reasoning_tokens is None or record.reasoning_tokens < record.completion_tokens for record in admissions)
        and len(set(inputs)) == 1 and max(outputs) <= 2*min(outputs))
    if not observation.get("isolated_run"):
        reason = (summary["stop_reason"] if observation.get("budget_limited") else
                  "Policy qualification requires one target and only policy traffic, with no restart readiness probes.")
        return outcome("unverified", reason)
    if settings.window_state == "unknown" or not settings.gateway_version:
        return outcome("unverified", "Gateway version or isolated fresh single-target policy window is unknown.")
    if settings.window_state != "isolated_fresh_single_counter":
        return outcome("unverified", "Policy qualification requires an operator-confirmed authoritative limiter counter.")
    if not observation.get("identities_distinct"):
        return outcome("unverified", "Distinct authenticated test credentials could not be established locally.")
    cutoff_reason = "Policy request timing could not be established within the observation window."
    if not recognized:
        if incomplete_window:
            return outcome("unverified", cutoff_reason)
        if affected_rejections:
            return outcome("unverified", "Rejection does not identify the declared gateway policy.")
        return outcome("unexercised", "No attributed policy rejection was observed within offered work.")
    windows = [record.gateway_evidence.retry_after_seconds for record in recognized]
    # LiteLLM v3 reports the configured window and truncates bucket start
    # timestamps to whole seconds. A fresh bucket can reset almost 1s early.
    if (any(window is None or window <= 1 for window in windows) or len(set(windows)) != 1
            or start is None or end is None or end < start or end - start >= (windows[0] - 1)*1000):
        return outcome("unverified", "Cannot establish a single policy window from Retry-After and the observation duration.")
    summary["effective_window_seconds"] = windows[0]
    allowance = declared.rpm
    if settings.capability == "priority_rpm":
        if not settings.named_reservations_confirmed:
            return outcome("unverified", "Operator confirmation of distinct configured named reservations is required.")
        if settings.saturation_cache_seconds != 0:
            return outcome("unverified", "Cached saturation can differ between gateway nodes; this check requires zero cache delay.")
        evidence = [record.gateway_evidence for record in recognized]
        if any(item.model_rpm != settings.model_rpm or item.priority_matches is not True for item in evidence):
            return outcome("unverified", "Priority tier or model RPM evidence does not match the declaration.")
        tpm_configs = {(item._model_tpm, item._model_tpm_unconfigured) for item in evidence}
        if len(tpm_configs) != 1:
            return outcome("unverified", "Model TPM configuration differs between rejections in the isolated window.")
        model_tpm, rpm_only_saturation = tpm_configs.pop()
        model_limits = {"model_rpm": settings.model_rpm, "model_tpm": model_tpm}
        if any(_scoped_limit_conflicts(record, model_limits) for record in [*admissions, *recognized]):
            return outcome("unverified", "Scoped model RPM or TPM headers contradict the policy rejection evidence.")
        if any(_scoped_limit_conflicts(record, {"priority_rpm": declared.rpm}) for record in recognized):
            return outcome("unverified", "Scoped priority RPM headers contradict the declared reservation.")
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
        strict_evidence_missing = "Strict-mode evidence does not establish the declared saturation threshold."
        rejected_ids = {record.id for record in recognized}
        earlier_readings = []
        ordered_rejections = sorted(recognized, key=lambda record: record.completed_ms if record.completed_ms is not None else inf)
        for rejection in ordered_rejections:
            bounds = rejection.gateway_evidence._saturation_bounds
            if bounds is None or bounds[1] < threshold_percent:
                return outcome("unverified", strict_evidence_missing)
            completed = _completed_before_dispatch(admissions, rejection)
            if completed * 100 > bounds[1] * settings.model_rpm:
                return outcome("unverified", "Reported saturation contradicts completed admissions in the isolated window.")
            # Both counters are nondecreasing in this window. Count only
            # admissions whose entire request fell between two known readings.
            preceding = [reading for reading in earlier_readings
                         if rejection.dispatch_ms is not None and reading[0] < rejection.dispatch_ms]
            prior_rpm_floor = max((count + _completed_before_dispatch(admissions, rejection, after_ms=finished)
                                   for finished, count, _ in preceding), default=0)
            minimum_rpm = max(completed, prior_rpm_floor)
            minimum_saturation = max(Fraction(bounds[0]), Fraction(minimum_rpm * 100, settings.model_rpm),
                                     max((saturation for _, _, saturation in preceding), default=0))
            if minimum_saturation > bounds[1]:
                return outcome("unverified", "Reported saturation contradicts earlier limiter evidence in the isolated window.")
            reported = rejection.gateway_evidence._reported_saturation
            # A recognized priority rejection changes neither counter. Other
            # failures may have been charged before the gateway replied.
            possible = sum(record.id not in rejected_ids and (
                record.dispatch_ms is None or rejection.completed_ms is None
                or record.dispatch_ms <= rejection.completed_ms
            ) for record in records)
            minimum_count = ceil(minimum_saturation * settings.model_rpm / 100)
            maximum_count = min(possible, floor(bounds[1] * settings.model_rpm / 100))
            least_matching_rpm = _least_reported_count(reported, settings.model_rpm, minimum_count, maximum_count)
            if least_matching_rpm is None:
                return outcome("unverified", "Reported saturation cannot match any possible model RPM count in the isolated window.")
            # LiteLLM reports the larger of the RPM and TPM ratios. If TPM
            # could produce that number, the printed value cannot prove the
            # RPM counter crossed the threshold. Use observed admissions.
            rpm_reading = (rpm_only_saturation or model_tpm is not None
                           and _least_reported_count(reported, model_tpm, 0, model_tpm) is None)
            if rpm_reading:
                if least_matching_rpm < crossing:
                    return outcome("unverified", strict_evidence_missing)
                minimum_rpm = least_matching_rpm
                minimum_saturation = Fraction(least_matching_rpm * 100, settings.model_rpm)
            elif minimum_rpm < crossing:
                return outcome("unverified", "Reported saturation may reflect TPM, and observed RPM admissions do not establish strict mode.")
            if rejection.completed_ms is not None:
                earlier_readings.append((rejection.completed_ms, minimum_rpm, minimum_saturation))
        if len(admissions) > settings.model_rpm:
            return outcome("observed_failure", "Total admitted requests exceeded the declared model RPM in the isolated window.", "fail")
        allowance = max(allowance, crossing)
    elif (any(record.gateway_evidence.request_limit != declared.rpm or record.gateway_evidence.request_remaining != 0
              for record in recognized)
          or any(_scoped_limit_conflicts(record, {"api_key_rpm": declared.rpm})
                 for record in [*affected_admissions, *recognized])):
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
        uncertain_prior = sum(record.identity == settings.affected_identity
                              and record not in prior_admissions and record not in recognized
                              and (record.dispatch_ms is None or record.dispatch_ms < first_rejection)
                              for record in records)
        if len(prior_admissions) + uncertain_prior >= declared.rpm:
            return outcome("unverified", "Prior requests with unknown admission could have consumed the declared RPM allowance.")
        return outcome("observed_failure", "Attributed rejection occurred before the declared RPM allowance was served.", "fail")
    if len(recognized) != len(affected_rejections):
        return outcome("unverified", "Affected identity received 429 responses that do not identify the declared gateway policy.")
    if incomplete_window:
        return outcome("unverified", cutoff_reason)
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
