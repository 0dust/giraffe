"""Run-owned counting and shared wording for repeated ordinary request timeouts."""
from copy import deepcopy


def format_timeout_deadline(seconds: float) -> str:
    return f"{seconds * 1000:g} ms" if seconds < 1 else f"{seconds:g}s"


def format_timeout_warning(payload: dict, check_label=None) -> str:
    parts = [f"{payload[key]} {text}" for key, text in (
        ("answer_started", "had started answering"),
        ("reasoning_only", "produced reasoning only"),
        ("no_output", "received no output")) if payload[key]]
    setting = " and ".join(
        "--request-timeout (request_timeout_seconds)" if path == "request_timeout_seconds" else path
        for path in payload.get("settings", [payload["setting"]]))
    fixtures = {item["setting"]: item for item in payload.get("fixture_settings", [])}
    if fixtures:
        edits = "; ".join(f'edit {item["field"]} on fixture {item["fixture_id"]} in {item["file"]}'
                          for item in fixtures.values())
        other = [path for path in payload.get("settings", [payload["setting"]]) if path not in fixtures]
        remedy = f"{edits}" + (f" and raise {' and '.join(other)}" if other else "") + ", then stop and rerun"
    else:
        remedy = f"stop and rerun with a higher {setting}"
    return (f'{payload["target"]}: {payload["timed_out"]} of {payload["finished"]} requests hit '
            f'the {format_timeout_deadline(payload["deadline_seconds"])} request timeout ({", ".join(parts)}). '
            'Giraffe does not extend timeouts during a run. To allow more time, '
            f'{remedy}; keep it if slower responses should count as failures.')


class TimeoutWatch:
    """Count finished ordinary requests without changing their deadlines or workload."""

    def __init__(self):
        self._groups = {}
        self._warnings = {}

    @property
    def warnings(self) -> list[dict]:
        return deepcopy(list(self._warnings.values()))

    def observe(self, record, elapsed_seconds: float, *, readiness=False, evidence_probe=False):
        reason = record.termination_reason or ""
        if (reason in {"user_stop", "run_duration_limit", "memory_limit", "error_threshold",
                       "endpoint_unresponsive", "workload_cancelled"} or reason.startswith("intentional_")
                or readiness or evidence_probe or record.intentional or record.scenario in {
                "deadline_probe", "client_deadline", "client_cancel"}):
            return None
        deadline = record.request_timeout_seconds
        if deadline is None:
            return None
        source = record.deadline_source
        group_key = (record.target, deadline, source)
        check = getattr(record, "timeout_check", None)
        setting = getattr(record, "timeout_setting", None) or (
            f"test_options.{check}.request_timeout_seconds" if source == "check" else
            "load_test.request_timeout_seconds" if source == "load" else "request_timeout_seconds")
        group = self._groups.setdefault(group_key, {
            "key": f"{record.target}|{float(deadline)}|{source}", "target": record.target,
            "deadline_seconds": float(deadline), "deadline_source": source,
            "setting": setting, "cli_flag": "--request-timeout" if source == "run" else None,
            "finished": 0, "timed_out": 0, "answer_started": 0,
            "reasoning_only": 0, "no_output": 0, "checks": [],
            "first_at_seconds": None, "updated_at_seconds": None, "update": 0})
        group["finished"] += 1
        if record.status == "timeout" and reason in {
                "request_timeout", "request_deadline", "read_timeout", "write_timeout",
                "pool_timeout", "transport_timeout"}:
            group["timed_out"] += 1
            settings = group.setdefault("settings", [])
            if setting not in settings:
                settings.append(setting)
            group["setting"] = settings[0]
            group["cli_flag"] = "--request-timeout" if settings == ["request_timeout_seconds"] else None
            fixture = getattr(record, "_timeout_fixture", None)
            if fixture:
                fixtures = group.setdefault("fixture_settings", [])
                if fixture not in fixtures:
                    fixtures.append(deepcopy(fixture))
            answer = record.answer_chars > 0 or record.first_output_ms is not None
            reasoning = (bool(record.reasoning) or record.first_reasoning_ms is not None
                         or bool(record.reasoning_tokens))
            group["answer_started" if answer else "reasoning_only" if reasoning else "no_output"] += 1
            group["checks"] = sorted(set(group["checks"]) | set(record.check_ids))
        if not group["update"] and (group["timed_out"] < 3 or group["timed_out"] / group["finished"] < .2):
            return None
        if not group["update"]:
            group["first_at_seconds"] = elapsed_seconds
        group["updated_at_seconds"] = elapsed_seconds
        group["update"] += 1
        payload = deepcopy(group)
        self._warnings.pop(group_key, None)
        self._warnings[group_key] = payload
        return deepcopy(payload)
