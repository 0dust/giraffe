"""One public workload check; preserve the existing scenario verdicts and evidence."""

from __future__ import annotations

from giraffe.models import CHECK_NAMES, CheckResult, RequestSpec, overall_status
from giraffe.overlap import NEEDS_STREAM, peak_evidenced_concurrency, scheduling_delay_windows

SCENARIOS = {
    "size_matrix": ("buckets", "Input/output size and concurrency matrix"),
    "mixed_interference": ("mixed", "Mixed workload interference"),
    "streaming_fairness": ("fairness", "Streaming fairness"),
    "token_stress": ("token_stress", "Token stress cases"),
}


def execution_config(config):
    """Expand the public selection only inside execution, never in saved config."""
    selected = []
    for name in config.checks:
        if name == "concurrency":
            selected.extend(SCENARIOS[scenario][0]
                            for scenario in config.concurrent_workloads.scenarios)
        else:
            selected.append(name)
    return config.model_copy(update={"checks": selected}, deep=True)


def consolidate_checks(run, config, checks):
    from giraffe.runner import _stats, _budget_guidance

    component_ids = {item[0] for item in SCENARIOS.values()}
    retained = [check for check in checks if check.id not in component_ids]
    for target in config.targets:
        components = {check.id: check for check in checks if check.target == target.name}
        scenarios = {}
        if "concurrency" in config.checks:
            for scenario in config.concurrent_workloads.scenarios:
                component, title = SCENARIOS[scenario]
                check = (stress_result(run, target) if scenario == "token_stress"
                         else components.get(component))
                if check is None:
                    check = CheckResult(id=component, target=target.name, title=title,
                                        status="inconclusive", required=True,
                                        summary=("Scenario was not reached within the shared run budget. To conclude, "
                                            + (getattr(run, "verdict_budget_guidance", None) or _budget_guidance(run.config))
                                            + " Allow more max_duration_seconds if the deadline stopped the run."))
                scenarios[scenario] = {**check.model_dump(mode="json"), "title": title}
        evidence = list(dict.fromkeys(rid for item in scenarios.values()
                                      for rid in item["evidence_ids"]))
        evidence_set = set(evidence)
        records = [record for record in run.records if record.id in evidence_set]
        status = overall_status([CheckResult.model_validate(item) for item in scenarios.values()], None)
        if not scenarios:
            status = "skipped"
        problems = sorted((item for item in scenarios.values() if item["status"] in {"fail", "inconclusive", "blocked"}),
                          key=lambda item: item["status"] != "fail")
        merged = CheckResult(
            id="concurrency", target=target.name, title=CHECK_NAMES["concurrency"],
            required="concurrency" in config.checks, status=status,
            summary=("Scenario results: " + "; ".join(
                f"{item['title']}: {item['status']}" for item in scenarios.values())
                + (f". {problems[0]['title']}: {problems[0]['summary']}" if problems else "")
                if scenarios else "Concurrent workloads were not selected."),
            metrics={**_stats(records), "scenarios": scenarios}, evidence_ids=evidence)
        # Keep the merged check at the old fairness position in the public list.
        index = next((i + 1 for i, item in enumerate(retained)
                      if item.target == target.name and item.id == "capacity"), len(retained))
        retained.insert(index, merged)
    for record in run.records:
        if component_ids.intersection(record.check_ids):
            record.check_ids = list(dict.fromkeys([*record.check_ids, "concurrency"]))
    return retained


def _prompt(case, filler_chars=0):
    if case.name == "long_input_summary":
        prefix = "Project notes. The box label is K7P4. The delivery is ready.\n"
        filler = ("The team reviewed the delivery notes. " * (filler_chars // 38 + 1))[:filler_chars]
        suffix = (f"\nSummarize the notes in about {case.output_tokens} tokens. "
                  "Include the box label and delivery status. Output only the summary.")
        return prefix + filler + suffix
    return "Write an extensive numbered story. Continue until the output limit."


def _spec(case, prompt, scenario, *, calibration=False, level=1):
    summary = case.name == "long_input_summary" and not calibration
    return RequestSpec(
        fixture_id=f"token_stress.{case.name}.v1", scenario=scenario,
        check_ids=["token_stress"], messages=[{"role": "user", "content": prompt}],
        max_tokens=1 if calibration else case.output_tokens, input_chars=len(prompt),
        scorer="contains" if summary else "none", expected="K7P4" if summary else None,
        workload={"case": case.name, "level": level,
                  "requested_input_tokens": case.input_tokens,
                  "requested_output_tokens": case.output_tokens,
                  "semantic_scope": ("Required box-label fact only; full summary quality is not assessed."
                                     if summary else "Generation length and serving behavior; answer quality unscored.")})


def _empty_case(case):
    return {"title": (f"{case.input_tokens:,} input / {case.output_tokens:,} output summary"
                       if case.name == "long_input_summary"
                       else f"Short input / {case.output_tokens:,} output generation"),
            "requested_input_tokens": case.input_tokens,
            "maximum_input_tokens": case.max_input_tokens,
            "requested_output_tokens": case.output_tokens,
            "minimum_output_tokens": case.min_output_tokens or case.output_tokens,
            "calibration_requests": [], "levels": {}, "actual_input_tokens": [],
            "actual_output_tokens": [], "coverage_gaps": [], "evidence_ids": [],
            "status": "inconclusive"}


async def token_stress(run, target, client):
    """Size input from returned usage and verify actual lengths at every level."""
    from giraffe.runner import _stats, _timing_violations, _missing_timing, _check_config

    settings = run.config.concurrent_workloads
    effective = _check_config(run.config, "concurrency")
    cases = run.observations[target.name].setdefault("token_stress", {"cases": {}})["cases"]
    for case in settings.stress_cases:
        info = cases[case.name] = _empty_case(case)
        required_context = (case.input_tokens or case.max_input_tokens or 128) + case.output_tokens
        if required_context > effective.context_limit:
            info["coverage_gaps"].append(
                f"Requires context_limit >= {required_context}; configured {effective.context_limit}.")
        if case.output_tokens > run.config.max_output_tokens:
            info["coverage_gaps"].append(
                f"Requires max_output_tokens >= {case.output_tokens}; configured {run.config.max_output_tokens}. No smaller substitute was run.")
        if info["coverage_gaps"]:
            continue
        prompt = _prompt(case)
        calibration_failed = False
        if case.input_tokens:
            filler_chars = case.input_tokens
            calibrated = False
            for _ in range(settings.calibration_attempts):
                prompt = _prompt(case, filler_chars)
                record = await run.request(target, client, _spec(
                    case, prompt, f"stress_calibration_{case.name}", calibration=True))
                if record is None:
                    break
                info["calibration_requests"].append(record.id)
                info["evidence_ids"].append(record.id)
                if not record.valid:
                    calibration_failed = record.status in {"failed", "timeout"}
                    info["coverage_gaps"].append(record.error or "Calibration inference failed.")
                    break
                actual = record.input_tokens
                if not actual:
                    info["coverage_gaps"].append("Input token usage unavailable; target length cannot be verified.")
                    break
                if abs(actual - case.input_tokens) <= case.input_tokens * settings.input_token_tolerance:
                    calibrated = True
                    break
                overhead = len(_prompt(case))
                filler_chars = max(1, min(case.input_tokens * 16,
                                         round(len(prompt) * case.input_tokens / actual) - overhead))
            if not calibrated:
                info["coverage_gaps"].append("Requested input token length was not calibrated within the shared budget.")
                info["status"] = "fail" if calibration_failed else "inconclusive"
                continue
        for level in settings.levels:
            scenario = f"stress_{case.name}_c{level}"
            planned = max(settings.samples_per_bucket, level)
            records = await run.group(target, client, [_spec(case, prompt, scenario, level=level)],
                                      scenario, count=planned,
                                      concurrency=level)
            row = _stats(records)
            row["planned_requests"] = planned
            # Client dispatch overlap is not server evidence; only overlapping output windows are.
            row["client_peak_inflight"] = max((record.overlap for record in records), default=0)
            row["achieved_overlap"] = peak_evidenced_concurrency(records, scheduling_delay_windows(run))
            row["evidence_ids"] = [record.id for record in records]
            row["coverage_gaps"] = gaps = []
            if len(records) < planned:
                gaps.append("The shared budget ended before all planned requests were sent.")
            if row["completed"] < effective.limits.min_samples:
                gaps.append("Insufficient successful samples.")
            if level > 1 and row["achieved_overlap"] < level:
                gaps.append("Requested concurrency was not observed in overlapping server output"
                            + ("." if any(record.stream for record in records) else f"; {NEEDS_STREAM}."))
            if any(record.input_tokens is None for record in records) or not records:
                gaps.append("Actual input token length is unavailable.")
            elif case.input_tokens and any(
                    abs(record.input_tokens - case.input_tokens) > case.input_tokens * settings.input_token_tolerance
                    for record in records):
                gaps.append("Observed input length differs from the requested target.")
            elif case.max_input_tokens and any(
                    record.input_tokens > case.max_input_tokens for record in records):
                gaps.append("Observed short input exceeds its configured token ceiling.")
            minimum = case.min_output_tokens or case.output_tokens
            if any(record.output_tokens is None or record.output_tokens < minimum for record in records):
                gaps.append("Requested output length was not exercised; a cap alone is not coverage.")
            if _missing_timing(records, effective.limits):
                gaps.append("Configured timing limits lack measurements.")
            from giraffe.timing import configured_metrics, qualified_violations, run_quality
            quality = run_quality(run, records, target.name, configured_metrics(effective.limits, "concurrency"))
            row["timing_quality"] = quality
            row["timing_violations"] = _timing_violations(records, effective.limits)
            row["qualified_timing_violations"] = qualified_violations(records, effective.limits, quality)
            if quality["status"] == "client_delayed" and row["timing_violations"]:
                gaps.append("Client scheduling delay prevents attributing timing violations to the endpoint.")
            row["output_cap_violations"] = [record.id for record in records
                if record.completion_tokens is not None and record.completion_tokens > record.requested_max_tokens]
            failed = any(record.status in {"failed", "timeout"} or record.score is False for record in records)
            failed |= bool(row["qualified_timing_violations"] or row["output_cap_violations"])
            failed |= bool(row["timing_violations"] and quality["status"] != "client_delayed")
            row["status"] = "fail" if failed else "inconclusive" if gaps else "pass"
            info["levels"][str(level)] = row
            info["evidence_ids"].extend(row["evidence_ids"])
            info["actual_input_tokens"].extend(row["actual_input_tokens"])
            info["actual_output_tokens"].extend(row["actual_output_tokens"])
            info["coverage_gaps"].extend(f"Concurrency {level}: {gap}" for gap in gaps)
        statuses = [row["status"] for row in info["levels"].values()]
        info["status"] = ("fail" if "fail" in statuses else "inconclusive"
                          if not statuses or "inconclusive" in statuses else "pass")


def stress_result(run, target):
    from giraffe.runner import _stats, _verdict_summary, _budget_guidance, _check_config

    observed = run.observations[target.name].get("token_stress", {}).get("cases", {})
    cases = {}
    for case in run.config.concurrent_workloads.stress_cases:
        info = observed.get(case.name) or _empty_case(case)
        if case.name not in observed:
            info["coverage_gaps"].append("Token stress case was not reached within the shared run budget.")
        cases[case.name] = info
    evidence = list(dict.fromkeys(rid for case in cases.values() for rid in case["evidence_ids"]))
    evidence_set = set(evidence)
    records = [record for record in run.records if record.id in evidence_set]
    statuses = [case["status"] for case in cases.values()]
    status = "fail" if "fail" in statuses else "inconclusive" if "inconclusive" in statuses else "pass"
    result = CheckResult(id="token_stress", target=target.name, title="Token stress cases",
                       required=True, status=status,
                       summary="; ".join(f"{name}: {case['status']}" + (
                           " — " + "; ".join(case["coverage_gaps"][:3]) if case["coverage_gaps"] else "")
                           for name, case in cases.items()),
                       metrics={**_stats(records), "cases": cases}, evidence_ids=evidence)
    if status in {"fail", "inconclusive"}:
        result.summary = _verdict_summary(result, _check_config(run.config, "concurrency"),
            records, getattr(run, "verdict_budget_guidance", None) or _budget_guidance(run.config))
    return result
