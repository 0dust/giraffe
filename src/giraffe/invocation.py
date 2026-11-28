"""Run-launch provenance; never expand environment references or read input files."""

from .deployment import REDACTED, redact_url, sanitize

# Only known CLI settings may retain values. New/abbreviated flags fail closed,
# so future inline prompts, headers or credentials cannot silently enter reports.
_VALUE_FLAGS = frozenset("""
--config --manifest --url --model --api-key-env --concurrency --simultaneous-limit
--default-concurrency --max-requests --max-duration --max-memory --request-timeout
--max-output-tokens --context-limit --samples --sustained-seconds --stop-after-errors
--unresponsive-after-failures --unresponsive-for --checks --deployment-file
--metrics-url --host-metrics-url --metrics-api-key-env --metrics-profile --fixtures
--request-pack --retention --ca-bundle --proxy --first-output-ms --latency-ms
--stream-gap-ms --min-output-tokens-per-second --max-error-rate --min-correctness
--case --target --regression-percent --regression-rate-points --baseline --output --save-config --output-format --runs-dir --restart
""".split())
_URL_FLAGS = frozenset({"--url", "--metrics-url", "--host-metrics-url", "--proxy"})


def _safe_value(flag: str, value: str) -> str:
    if flag in _URL_FLAGS:
        value = redact_url(value)
    return sanitize(value)


def cli_invocation(argv: list[str]) -> dict:
    """Keep argument order and equals syntax, redacting unknown option values."""
    result = []
    value_flag = None
    for index, arg in enumerate(argv):
        if index == 0 and arg in {"run", "cold-start", "try-example"}:
            result.append(arg)
        elif arg.startswith("--"):
            flag, equals, value = arg.partition("=")
            value_flag = flag if flag in _VALUE_FLAGS else None
            if equals:
                value = _safe_value(flag, value) if value_flag else REDACTED
                result.append(f"{sanitize(flag)}={value}")
                value_flag = None
            else:
                result.append(sanitize(flag))
        else:
            result.append(_safe_value(value_flag, arg) if value_flag else REDACTED)
            value_flag = None
    return {"source": "cli", "argv": result}


def safe_invocation(value) -> dict:
    """Normalize imported/legacy provenance at the report export boundary."""
    if isinstance(value, dict):
        argv = value.get("argv")
        if value.get("source") == "cli" and isinstance(argv, list) and all(
            isinstance(arg, str) for arg in argv
        ):
            return cli_invocation(argv)
        if value.get("source") in {"web_api", "python_api"}:
            return {"source": value["source"], "argv": None}
    return {"source": "unknown", "argv": None}
