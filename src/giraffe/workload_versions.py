"""Explicit versions of the request workloads that baseline identity covers.

Bump a workload's number when the requests it sends change: prompt text, caps,
schedule, arrival pattern, dispatch order or anything else a server would see.
Do not bump for messages, summaries, scoring diagnostics, refactors or comments.
tests/test_workload_versions.py pins a digest of each workload's generated
requests and fails when they change without a version bump.
"""

WORKLOAD_VERSIONS: dict[str, int] = {
    "prefix": 1,
    "consistency": 1,
    "tools": 1,
    "fixed_sessions": 1,
    "session_plan": 1,
    "multi_user": 1,
    "gateway_access": 1,
    "gateway_policy": 1,
    "scale_down": 1,
    "load_test": 1,
    "logprob": 1,
    "tool_conversations": 1,
}
