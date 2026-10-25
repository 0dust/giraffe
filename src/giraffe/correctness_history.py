"""Prior scored answers are advisory evidence, never a prediction of impossibility."""

from collections import defaultdict

from .deployment import fixture_hash
from .fixtures import FIXTURE_VERSION, builtin_fixtures, deterministic_options
from .models import RunConfig
from .request_pack import active_pack, judging_identity, pack_specs


def fixture_keys(config):
    if config.custom_fixtures:
        return {}
    if active_pack(config):
        specs = pack_specs(config)
    elif "correctness" in config.checks:
        effective = config.effective_check("correctness")
        current = config.model_copy(update={key: value for key, value in effective.items() if key != "limits"})
        specs = builtin_fixtures(current, check_id="correctness")["correctness"]
        specs = [spec.model_copy(update={"options": deterministic_options(current, spec.options)}) for spec in specs]
    else:
        return {}
    load = config.load_test.model_dump(mode="json") if "load" in config.checks else None
    if load:
        load.pop("pass_criteria", None)
        for migrated in load.get("migrated_tests", []):
            migrated["settings"].pop("pass_criteria", None)
    context = fixture_hash({"version": 2, "fixtures": FIXTURE_VERSION, "checks": config.checks, "load": load,
        "judging": judging_identity(config), "stream": config.stream, "options": config.request_options,
        "concurrency": config.concurrency, "default_concurrency": config.default_concurrency,
        "correctness_concurrency": config.effective_check("correctness")["concurrency"],
        "overlap_models": config.overlap_models,
        "targets": [{key: target.model_dump(mode="json").get(key) for key in
                     ("name", "url", "model", "tokenizer", "identity", "route", "parent", "api_key_env", "headers_env")}
                    for target in config.targets]})
    return {spec.fixture_id: {"key": fixture_hash({"context": context,
        "messages": spec.messages, "scorer": spec.scorer, "expected": spec.expected,
        "expected_provided": spec.expected_provided, "schema": spec.schema_, "rubric": spec.rubric,
        "max_tokens": spec.max_tokens, "timeout": float(spec.request_timeout_seconds or config.request_timeout_seconds),
        "options": spec.options}), "scorer": spec.scorer,
        "case_id": spec.workload.get("case_id")} for spec in specs if spec.scorer != "none"}


def measurements(report, target_name):
    try:
        config = RunConfig.model_validate(report.manifest.get("config", {}))
    except ValueError:
        return []
    if config.restart_target:
        return []
    keys = fixture_keys(config)
    groups = defaultdict(list)
    for record in report.requests:
        if (record.target == target_name and record.fixture_id in keys and record.score is not None
                and record.status == "completed" and record.valid and not record.error
                and not record.evaluation_error and not record.intentional):
            groups[record.fixture_id].append(record)
    return [{"family": "correctness_history", "fixture_id": fixture_id, "fixture_key": keys[fixture_id]["key"],
             "scorer": keys[fixture_id]["scorer"], "scored": len(records),
             "failed": sum(record.score is False for record in records),
             "failed_request_ids": [record.id for record in records if record.score is False][:3]}
            for fixture_id, records in groups.items()]


def guidance(config, observations):
    from .latency import _datetime

    if "correctness" not in config.checks and not active_pack(config):
        return []
    if not any(entry.get("family") == "correctness_history"
               for entries in (observations or {}).values() for entry in entries):
        return [{"target": target.name, "status": "unknown"} for target in config.targets]
    keys = fixture_keys(config)
    rows = []
    for target in config.targets:
        by_fixture = defaultdict(list)
        for entry in (observations or {}).get(target.name, []):
            if (entry.get("family") == "correctness_history" and entry.get("source_target") == target.name
                    and isinstance(entry.get("fixture_id"), str)):
                by_fixture[entry.get("fixture_id")].append(entry)
        for fixture_id, identity in keys.items():
            matches = []
            for entry in by_fixture[fixture_id]:
                if (entry.get("fixture_key") != identity["key"] or entry.get("deployment_known") is not True):
                    continue
                scored, failed, ids = entry.get("scored"), entry.get("failed"), entry.get("failed_request_ids")
                if (type(scored) is int and type(failed) is int and 0 <= failed <= scored and scored > 0
                        and isinstance(ids, list) and len(ids) == min(3, failed)
                        and all(isinstance(value, str) and value for value in ids)
                        and isinstance(entry.get("run_id"), str) and isinstance(entry.get("finished_at"), str)):
                    matches.append(entry)
            if not matches:
                rows.append({"target": target.name, "fixture_id": fixture_id, "status": "unknown"})
                continue
            previous = max(matches, key=lambda entry: _datetime(entry["finished_at"]))
            failed = previous["failed"]
            message = (f"{target.name}: {fixture_id} had {failed}/{previous['scored']} scored answers fail in run "
                f"{previous['run_id']} ({previous['finished_at']}) with matching question, scorer and request settings. "
                "Configured deployment metadata matches; runtime continuity is unverified. "
                "Review the saved answers before repeating the full run. This does not predict the next answer; criteria are unchanged.")
            rows.append({"target": target.name, "fixture_id": fixture_id, "case_id": identity["case_id"],
                "status": "warning" if failed else "observed", "run_id": previous["run_id"],
                "finished_at": previous["finished_at"], "scored": previous["scored"], "failed": failed,
                "failed_request_ids": previous["failed_request_ids"], "message": message if failed else None})
    return rows
