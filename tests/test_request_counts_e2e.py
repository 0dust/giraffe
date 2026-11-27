"""Reconcile CLI request accounting against real target inference POSTs."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from tests.fake_endpoint import FakeEndpoint


@pytest.mark.parametrize("command,budget,skip_preflight,expected,reason", [
    ("cold-start", 2, True, (2, 1, 1, 0), "restart_readiness"),
    ("cold-start", 1, True, (1, 0, 1, 0), "restart_readiness"),
    ("run", 1, False, (2, 1, 1, 1), "preflight"),
    ("run", 1, True, (1, 1, 0, 1), None),
])
def test_cli_counts_match_target_posts(tmp_path, command, budget, skip_preflight, expected, reason):
    with FakeEndpoint() as endpoint:
        config = tmp_path / "config.json"
        config.write_text(json.dumps({
            "targets": [{"name": "local", "url": endpoint.url, "model": "fake-model",
                         "restart_command": ["true"]}],
            "checks": ["access"], "stream": True, "request_timeout_seconds": 3,
            "max_duration_seconds": 20,
        }))
        output = tmp_path / "run"
        args = [sys.executable, "-m", "giraffe.cli", command, "--config", str(config),
                "--checks", "access", "--max-requests", str(budget), "--output", str(output),
                "--min-free-disk", "0", "--quiet"]
        if command == "cold-start":
            args.extend(["--restart", "local"])
        if skip_preflight:
            args.append("--skip-preflight")
        result = subprocess.run(args, capture_output=True, text=True, timeout=45,
                                cwd=Path(__file__).resolve().parents[1])
        report_path = output / "report.json"
        assert report_path.is_file(), result.stdout + result.stderr
        report = json.loads(report_path.read_text())
        counts = report["observations"]["request_counts"]
        assert counts["sent"] == len(endpoint.requests)
        assert counts["complete"] is True and counts["available"] is True
        assert counts["budget_counted_unsent"] == 0
        assert counts["sent"] == counts["budget_counted"] - counts["budget_counted_unsent"] + counts["budget_exempt"]
        assert tuple(counts[key] for key in ("sent", "budget_counted", "budget_exempt",
                                              "saved_observations")) == expected
        expected_reasons = {reason: 1} if reason else {}
        assert counts["budget_exempt_by_reason"] == expected_reasons
        assert counts["targets"]["local"] == {key: value for key, value in counts.items()
                                               if key not in {"targets", "complete", "available"}}
        assert len(report["requests"]) - sum(record["scenario"] in {
            "readiness", "performance_evidence_probe"} for record in report["requests"]
        ) == counts["saved_observations"]
