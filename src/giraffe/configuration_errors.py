"""Safe, shared configuration diagnostics for HTTP and CLI boundaries."""

from __future__ import annotations

import math
import re
from typing import Any

from giraffe.models import CATALOGUE_NAMES, ConcurrentLoad, LoadTest, RunConfig, Sessions


def configuration_errors(items: list[dict], body: Any) -> list[dict]:
    """Expose setting destinations without echoing arbitrary (possibly secret) input.

    Structured fields may be relative to a nested model or already absolute.
    Normalize their values and actions together. Only finite numbers and booleans are
    exposed; arbitrary input, headers and credentials never enter diagnostics.
    """
    from giraffe.capabilities import configuration_limits
    from giraffe.caps import cap_explanations

    limits = configuration_limits(RunConfig.model_json_schema())
    policies = cap_explanations(limits)
    config = body.get("config", body) if isinstance(body, dict) else {}
    if not isinstance(config, dict):
        config = {}
    labels = {"max_requests": "Stop after this many requests", "concurrency": "Limit simultaneous requests", "max_output_tokens": "Output cap (tokens)",
              "request_timeout_seconds": "Request timeout (seconds)",
              "max_duration_seconds": "Stop test traffic after", "max_memory_mb": "Memory limit (RAM)", "concurrent_load.schedule.levels": "Concurrency levels",
              "consistency.levels": "Concurrency levels", "sessions.concurrency": "Session concurrency",
              "traffic.max_in_flight": "Capacity requests at once"}

    def numeric(value):
        return type(value) is int or type(value) is float and math.isfinite(value)

    def number(value):
        if type(value) is bool:
            return int(value)
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                return None
            if math.isfinite(value) and value.is_integer():
                value = int(value)
        return value if numeric(value) else None

    def value(path):
        result = config
        for key in path.split("."):
            result = (result.get(key) if isinstance(result, dict) else
                      result[int(key)] if isinstance(result, list) and key.isdigit() and int(key) < len(result) else None)
        if result is None:
            parts = path.split(".")
            model = RunConfig if len(parts) == 1 else {"sessions": Sessions, "concurrent_load": ConcurrentLoad, "load_test": LoadTest}.get(parts[0])
            if model and len(parts) <= 2 and parts[-1] in model.model_fields:
                result = model.model_fields[parts[-1]].get_default(call_default_factory=True)
        if result is None and path.startswith("load_test."):
            result = LoadTest().model_dump()
            for key in path.split(".")[1:]:
                result = result.get(key) if isinstance(result, dict) else None
        if result is None and path.startswith("concurrent_load."):
            result = ConcurrentLoad().model_dump()
            for key in path.split(".")[1:]:
                result = result.get(key) if isinstance(result, dict) else None
        if isinstance(result, list):
            values = [number(item) for item in result]
            return values if all(numeric(item) for item in values) else None
        return number(result)

    roots = set(RunConfig.model_fields)

    def sensitive(path):
        return any(part in {"api_key", "api_keys", "api_key_env", "authorization", "password", "secret",
                            "headers", "request_options", "messages", "proxy"} for part in path.lower().split("."))

    def evidence(setting):
        return type(setting) is bool or numeric(setting) or isinstance(setting, list) and all(numeric(item) for item in setting)

    def destinations(path, fields):
        parts = path.split(".") if path else []
        # A custom error may attach its loc to any one of its sibling fields;
        # model-level errors instead have just the parent model as their loc.
        matching = [field.split(".") for field in fields
                    if len(parts) >= len(field.split(".")) and parts[-len(field.split(".")):] == field.split(".")]
        suffix = max(matching, key=len, default=[])
        prefix = parts[:-len(suffix)] if suffix else parts

        def absolute(field):
            if field.split(".")[0] in roots or field == "config":
                return field
            return ".".join([*prefix, field])

        return absolute

    def edit_label(field):
        if field in labels:
            return labels[field]
        if field.startswith("test_options."):
            parts = field.split(".")
            return CATALOGUE_NAMES.get(parts[1], parts[1]) + " · " + parts[-1].replace("_", " ")
        return policies.get(re.sub(r"\.\d+(?=\.|$)", "[]", field), {}).get("label", field.replace("_", " "))

    errors = []
    for item in items:
        loc, message = list(item["loc"]), item["msg"].removeprefix("Value error, ").removeprefix("Assertion failed, ")
        parts = loc[1:] if loc[:1] == ["body"] else loc
        parts = parts[1:] if parts[:1] == ["config"] else parts
        path = ".".join(map(str, parts))
        if item["type"] == "missing" and parts[-1:] == ["name"] and "targets" in parts:
            message = "Target name is required; add e.g. name: local"
        if path == "config_version":
            message = ("Unsupported configuration version. Use config_version: 2; "
                       "older unversioned files can omit config_version.")
        fields = [path] if path else []
        override = re.search(r"test_options\.([a-z_]+)\.([a-z_]+) must not exceed", message)
        levels = re.search(r"(consistency)\.levels must not exceed", message)
        context = item.get("ctx") if isinstance(item.get("ctx"), dict) else {}
        structured = context.get("fields")
        structured = structured if isinstance(structured, list) and structured and all(isinstance(field, str) and field for field in structured) else None
        absolute = destinations(path, structured) if structured else lambda field: field
        if structured:
            fields = list(dict.fromkeys(absolute(field) for field in structured))
        elif path.startswith("load_test."):
            from giraffe.caps import cap_policy
            label = cap_policy(path)[0]
            if message.startswith("Input should be "):
                message = label + " must be " + message.removeprefix("Input should be ").rstrip(".") + "."
            elif item["type"] == "extra_forbidden":
                message = "Remove the unrecognized setting under Load test."
            elif item["type"] != "value_error":
                message = label + ": " + message
        elif path.startswith("concurrent_load."):
            parts = path.split(".")[1:]
            label = {"schedule.levels": "Concurrency levels", "schedule.duration_seconds": "Send new requests for",
                "combinations.selected": "Input/output combinations", "combinations.enabled": "Input → output combinations",
                "mixed.shares": "Share of requests", "mixed.seed": "Mixed load seed",
                "interference.trigger": "Introduce the long input", "interference.trigger_chunks": "Answer chunks per short stream",
                "interference.max_slowdown": "Maximum slowdown vs short-only control",
                "stress.min_output_percent": "Minimum output length (% of cap)",
                "pass_criteria.first_output_p95_ms": "First answer p95 at most (ms)", "pass_criteria.response_p95_ms": "Response p95 at most (ms)",
                "pass_criteria.max_error_percent": "Errors allowed (%)", "pass_criteria.min_correct_percent": "Correct answers required (%)",
                "pass_criteria.min_valid_responses": "Minimum valid responses per test point",
                "input_token_tolerance": "Input token tolerance", "calibration_attempts": "Calibration attempts"}.get(".".join(parts[:2]), "Concurrent load")
            if parts[0] == "profile" and len(parts) >= 3 and parts[1] in {"short", "medium", "long"}:
                label = parts[1].capitalize() + (" input token target" if parts[2] == "input_tokens" else " output token cap")
            if item["type"] == "literal_error" and parts[:2] == ["interference", "trigger"]:
                message = "Introduce the long input after the short streams begin responding or after a number of answer chunks."
            elif item["type"] == "extra_forbidden":
                message = "Remove the unrecognized setting under Concurrent load."
            elif message.startswith("Input should be "):
                message = label + " must be " + message.removeprefix("Input should be ").rstrip(".") + "."
            elif item["type"] != "value_error":
                message = label + ": " + message
        elif override:
            path = f"test_options.{override[1]}.{override[2]}"
            fields = [path, override[2]]
            label = "requests at once" if override[2] == "concurrency" else labels.get(override[2], override[2]).lower()
            message = f"{CATALOGUE_NAMES.get(override[1], override[1])} {label} {value(path)} exceeds {labels.get(override[2], override[2])} {value(override[2])}."
        elif levels:
            path = levels[1] + ".levels"
            fields = [path, "concurrency"]
            message = f"Concurrency level {max(value(path) or [0])} exceeds Limit simultaneous requests {value('concurrency')}."
        elif message.startswith("traffic.max_in_flight must not exceed"):
            fields = ["traffic.max_in_flight", "concurrency"]
            message = f"Capacity requests at once {value(fields[0])} exceeds Limit simultaneous requests {value('concurrency')}."
        elif message.startswith("session concurrency must not exceed"):
            fields = ["sessions.concurrency", "concurrency"]
            message = f"Session concurrency {value(fields[0])} exceeds Limit simultaneous requests {value('concurrency')}."
        elif message.startswith("session workload output budgets"):
            ceiling = value("max_output_tokens")
            fields = [f"sessions.workloads.{index}.max_tokens" for index, workload in enumerate(config.get("sessions", {}).get("workloads", []))
                      if numeric(value(f"sessions.workloads.{index}.max_tokens")) and numeric(ceiling)
                      and value(f"sessions.workloads.{index}.max_tokens") > ceiling] + ["max_output_tokens"]
            message = f"Task output caps {', '.join(str(value(field)) for field in fields[:-1])} tokens exceed Output cap {ceiling} tokens."
        elif message.startswith("session output budget"):
            fields = ["sessions.max_tokens", "max_output_tokens"]
            message = f"Output cap per turn {value(fields[0])} tokens exceeds Output cap {value('max_output_tokens')} tokens."
        elif not fields:
            fields = ["config"]  # Advanced JSON is the destination for unmapped model errors.
        elif path in labels:
            message = f"{labels[path]}: {message}"
        # Schema errors and custom list validators share the field's cap policy.
        cap_path = fields[0] if structured else path
        canonical = re.sub(r"\.\d+(?=\.|$)", "[]", cap_path)
        policy = policies.get(canonical, {})
        bounds = limits.get(canonical, {})
        if not bounds and canonical.endswith("[]"):
            bounds = limits.get(canonical[:-2], {}).get("items", {})
        setting = value(cap_path)
        supplied_setting = config
        for part in cap_path.split("."):
            supplied_setting = (supplied_setting.get(part) if isinstance(supplied_setting, dict) else
                supplied_setting[int(part)] if isinstance(supplied_setting, list) and part.isdigit() and int(part) < len(supplied_setting) else None)
        cap_key = {"less_than_equal": "maximum", "too_long": "maxItems" if isinstance(supplied_setting, list) else "maxLength",
                   "greater_than": "exclusiveMinimum", "string_too_long": "maxLength"}.get(item["type"])
        if canonical.endswith("[]") and cap_key in {"maximum", "exclusiveMinimum"}:
            # Indexed array diagnostics retain the same each-entry policy as
            # the list editor; its maximum is not a maximum for the whole list.
            array_policy = policies.get(canonical[:-2], {})
            item_message = array_policy.get("errors", {}).get("item_" + cap_key)
            if item_message:
                policy = {**policy, "errors": {**policy.get("errors", {}), cap_key: item_message}}
        if not cap_key and isinstance(setting, list):
            items = bounds.get("items", {})
            if "maximum" in items and any(numeric(n) and n > items["maximum"] for n in setting):
                cap_key = "item_maximum"
            elif "exclusiveMinimum" in items and any(numeric(n) and n <= items["exclusiveMinimum"] for n in setting):
                cap_key = "item_exclusiveMinimum"
            elif "maxItems" in bounds and len(setting) > bounds["maxItems"]:
                cap_key = "maxItems"
        cap_message = policy.get("errors", {}).get(cap_key)
        if cap_message:
            message = cap_message
            fields = [cap_path]
        safe_fields = set(labels) | {"samples", "max_requests", "sessions.max_tokens"} | set(fields if override or levels or structured else [])
        safe_fields.update(field for field in fields if field.startswith(("concurrent_load.", "load_test."))
                           or re.fullmatch(r"sessions\.workloads\.\d+\.max_tokens", field))
        values = {field: setting for field in fields if field in safe_fields and not sensitive(field)
                  and (setting := value(field)) is not None and evidence(setting)}
        if cap_message:
            if setting is not None and evidence(setting):
                values[cap_path] = setting
            elif isinstance(supplied_setting, (list, str)):
                values["observed_length"] = len(supplied_setting)
        supplied = context.get("values")
        if isinstance(supplied, dict):
            for field, setting in supplied.items():
                if isinstance(field, str) and not sensitive(field) and evidence(setting):
                    # Computed evidence (planned arrivals, required sockets, etc.)
                    # stays a metric; actual relative setting paths get prefixed.
                    field = absolute(field) if structured and field in structured else field
                    values[field] = setting
        actions = []
        for action in context.get("actions", []) if isinstance(context.get("actions"), list) else []:
            if not isinstance(action, dict) or not isinstance(action.get("label"), str):
                continue
            action_fields = action.get("fields", [action.get("field")])
            if not isinstance(action_fields, list) or not action_fields or not all(isinstance(field, str) and field for field in action_fields):
                continue
            action_fields = [absolute(field) for field in action_fields]
            if any(sensitive(field) or field not in fields for field in action_fields):
                continue
            result = {"label": action["label"]}
            result.update({"fields": action_fields} if "fields" in action else {"field": action_fields[0]})
            if "value" in action:
                if not numeric(action["value"]) and type(action["value"]) is not bool:
                    continue
                result["value"] = action["value"]
            actions.append(result)
        if not actions:
            actions = [{"label": "Edit " + edit_label(field), "field": field} for field in fields]
        error = {"loc": loc, "msg": message, "field": fields[0], "fields": fields,
                 "values": values, "actions": actions}
        if isinstance(context.get("note"), str):
            error["note"] = context["note"]
        errors.append(error)
    return errors
