"""Run-owned counting and shared wording for repeated ordinary request timeouts."""
from copy import deepcopy


def format_timeout_warning(payload: dict, check_label=None) -> str:
    parts = [f"{payload[key]} {text}" for key, text in (
        ("answer_started", "had started answering"),
        ("reasoning_only", "produced reasoning only"),
        ("no_output", "received no output")) if payload[key]]
    setting = " and ".join(
        "--request-timeout (request_timeout_seconds)" if path == "request_timeout_seconds" else path
        for path in payload.get("settings", [payload["setting"]]))
    return (f'{payload["target"]}: {payload["timed_out"]} of {payload["finished"]} requests hit '
            f'the {payload["deadline_seconds"]:g}s request timeout ({", ".join(parts)}). '
            'Giraffe does not extend timeouts during a run. To allow more time, stop and rerun '
            f'with a higher {setting}; keep it if slower responses should count as failures.')


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
            if settings[0].startswith("custom_fixtures ("):
                group["setting_field"] = "custom_fixtures"
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
