"""Named configuration and runtime bounds.

Schema fields reference these constants; capabilities derives field bounds from
that schema and exports aggregate caps alongside them.
"""

MAX_SIMULTANEOUS_REQUESTS = 512
MAX_ARRIVAL_REQUESTS = 10_000
MAX_BURST_SIZE = 1024
MAX_PENDING_ARRIVALS = 256
MAX_TRAFFIC_RATE = 10_000
MAX_ARRIVAL_RATE = 1000
MAX_STAGES = 16
MAX_STAGE_SECONDS = 3600
MAX_SESSIONS = 32
MAX_SESSION_TURNS = 32
MAX_SESSION_TURN_CHARS = 8192
MAX_LIVE_REQUESTS = 4096
MAX_LIVE_CONDITIONS = 2048
MAX_LIVE_EVENTS = 100
MAX_LIVE_PHASES = 1024
MAX_SERVER_ID_CHARS = 4096
MAX_METRICS_BODY_BYTES = 1024 * 1024
MAX_METRICS_LABELS = 16
MAX_METRICS_LABEL_CHARS = 256
MAX_DEPLOYMENT_HISTORY = 32
MAX_DEPLOYMENT_BODY_BYTES = 65536
MAX_SANITIZE_DEPTH = 8
MAX_REPORT_SANITIZE_DEPTH = 24
MAX_SANITIZE_KEYS = 128
MAX_SANITIZE_KEY_CHARS = 128
MAX_SANITIZE_ITEMS = 256
MAX_SANITIZE_STRING_CHARS = 8192
MAX_LAUNCH_ARGUMENTS = 256
MAX_PREFLIGHT_HEADER_CHARS = 8192
MAX_PREFLIGHT_BODY_BYTES = 1024 * 1024
MAX_JUDGE_REASON_CHARS = 2000
MAX_LOGPROB_EXAMPLES = 16
MAX_TOOL_CONFIG_BYTES = 65536
MAX_TOOL_RESULT_BYTES = 65536
MAX_DEPLOYMENT_SCOPE_CHARS = 512
MAX_OLLAMA_HEADER_BYTES = 512 * 1024
MAX_SESSION_CREDENTIALS = 64
MAX_WEB_IDENTIFIER_CHARS = 160
MAX_MIGRATION_NOTES = 1000
MAX_BASELINE_NAME_CHARS = 120
MAX_DATASET_PREVIEW_ROWS = 5
MAX_TOOL_CALLS = 16
MAX_TOOL_NAME_CHARS = 64
MAX_HTML_PREVIEW_CHARS = 1200
MAX_HTML_JSON_PREVIEW_CHARS = 6000
DEFAULT_HTML_REQUEST_DETAILS = 25
MAX_FAILURE_SUMMARY_TITLE_CHARS = 256
MAX_FAILURE_SUMMARY_CHARS = 2048
MAX_REPORT_TOP_LEVEL_MEMBERS = 64

MAX_PLANNED_ARRIVALS = 100_000
MAX_REQUEST_PACK_BYTES = 1024 * 1024
MAX_REQUEST_PACK_CASES = 1000


# Explanations carry policy, not a second copy of schema maxima. Capabilities and
# boundary diagnostics render these same descriptions against schema-derived bounds.
_CAP_LABELS = {
    "max_duration_seconds": "Stop test traffic after (seconds)",
    "concurrency": "Limit simultaneous requests", "default_concurrency": "Default check concurrency",
    "requests": "Arrival count", "burst_size": "Burst size", "max_pending": "Pending arrivals",
    "requests_per_second": "Arrival rate (requests/second)", "rates": "Sending rates (requests/second)",
    "max_in_flight": "In-flight requests", "duration_seconds": "Stage duration (seconds)",
    "sessions": "Conversation count", "turns": "Conversation turns", "workloads": "Conversation workloads",
    "name": "Name", "weight": "Weight", "delay_seconds": "Pause between turns (seconds)",
    "prefix_chars": "Shared prefix (characters)", "repeats": "Prefix repeats", "history_turns": "History turns",
    "repetitions": "Consistency repetitions", "prompts_per_shape": "Prompts per shape",
    "levels": "Concurrency levels", "input_tokens": "Input token target", "output_tokens": "Output token cap",
    "calibration_attempts": "Calibration attempts", "trigger_chunks": "Answer chunks before the long input",
    "long_input_chars": "Long question (characters)", "long_output_words": "Long answer (words)",
    "cancel_after_chunks": "Chunks before cancellation", "cleanup_seconds": "Cleanup wait (seconds)",
    "trace_wait_seconds": "Trace wait (seconds)", "top_logprobs": "Top logprobs",
    "priming_rounds": "Priming rounds", "judges": "AI evaluators",
}


def cap_policy(path):
    """Return a readable setting name and the reason for its retained bounds."""
    path = path.removesuffix("[]")
    name = path.rsplit(".", 1)[-1]
    label = _CAP_LABELS.get(name, name.replace("_", " ").capitalize())
    if name in {"concurrency", "default_concurrency", "max_in_flight", "levels"}:
        return label, "product policy", "the measured reliable client-load ceiling"
    if path == "arrivals.max_pending":
        return label, "product policy", "keeping queued dispatch delay visible"
    if path in {"arrivals.requests_per_second", "traffic.rates", "load_test.patterns.rates"}:
        return label, "implementation constraint", "the arrival scheduler; accepted rates are not demonstrated delivery capacity"
    if name == "duration_seconds":
        if path.startswith(("concurrent_load.", "load_test.")):
            label = "Sending window (seconds)"
        return label, "product policy", "the length of an individual sending window"
    if ".limits." in path or path.startswith("limits.") or ".pass_criteria." in path:
        return label, "implementation constraint", "the meaning and scale of this pass criterion"
    if path.startswith("sessions."):
        return label, "software safety bound", "conversation history memory and profile expansion"
    if path.startswith("prefix."):
        return label, "software safety bound", "repeated prompt and history generation"
    if path.startswith("consistency."):
        return label, "software safety bound", "repeated requests and comparison evidence"
    if path.startswith(("concurrent_load.", "load_test.")):
        return label, "software safety bound", "generated payloads, calibration and workload expansion"
    if path.startswith("arrivals."):
        return label, "software safety bound", "memory and scheduling"
    if path.startswith("traffic."):
        return label, "software safety bound", "schedule and generated-payload memory"
    return label, "software safety bound", "bounded configuration, payload and evidence processing"


def cap_explanations(limits):
    """Explain each retained maximum using the actual capability bound values."""
    explanations = {}
    for path, bounds in limits.items():
        if path.startswith("runtime."):
            continue
        label, kind, reason = cap_policy(path)
        messages, descriptions = {}, []
        ending = f" — {'an' if kind == 'implementation constraint' else 'a'} {kind} on {reason}; exceeding it rejects the configuration. See docs/limits.md."
        for key, unit in (("maximum", ""), ("maxItems", " entries"), ("maxLength", " characters")):
            if key in bounds:
                maximum = format(bounds[key], ",") if type(bounds[key]) is int else format(bounds[key], ",g")
                if key == "maximum" and path.rsplit(".", 1)[-1] in {"min_correctness", "max_error_rate"}:
                    maximum += f" ({bounds[key] * 100:g}%)"
                phrase = (f"can contain at most {maximum}{unit}" if unit else f"can be at most {maximum}")
                bound_ending = (" — a software safety bound on workload combinations and evidence growth; exceeding it rejects the configuration. See docs/limits.md."
                                if key == "maxItems" and path in {"traffic.rates", "consistency.levels", "concurrent_load.schedule.levels"} else ending)
                messages[key] = label + " " + phrase + bound_ending
                descriptions.append(messages[key])
        item = bounds.get("items", {})
        for key, unit in (("maximum", ""), ("maxLength", " characters")):
            if key in item:
                maximum = format(item[key], ",") if type(item[key]) is int else format(item[key], ",g")
                phrase = f"each entry can be at most {maximum}{unit}"
                messages["item_" + key] = label + ": " + phrase + ending
                descriptions.append(messages["item_" + key])
        for key, source in (("exclusiveMinimum", bounds), ("item_exclusiveMinimum", item)):
            if "exclusiveMinimum" in source:
                minimum = format(source["exclusiveMinimum"], ",g")
                messages[key] = label + (": each entry" if key.startswith("item_") else "") + f" must be greater than {minimum}."
        if descriptions or messages:
            explanations[path] = {"label": label, "kind": kind, "reason": reason, "errors": messages,
                "help": " ".join(descriptions)}
    # An item validator's indexed location must use the same bound as its array.
    for path, bounds in list(limits.items()):
        if "items" in bounds:
            item_path = path + "[]"
            item_explanation = cap_explanations({item_path: bounds["items"]})
            explanations.update(item_explanation)
    # Probability evidence is edited as one JSON setting in the existing panel.
    prefix = "consistency.logprobs"
    help_text = " ".join(value["help"] for path, value in explanations.items()
                         if path.startswith(prefix + ".") and value["help"])
    if help_text:
        explanations[prefix] = {"label": "Probability evidence", "help": help_text}
    return explanations
