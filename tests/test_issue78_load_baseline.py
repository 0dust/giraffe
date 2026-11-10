"""Compare an actual pre-output-budget Load report with current generated work.

Regenerate the baseline using Python 3.12 and the dev dependencies:
    mkdir -p /tmp/issue78-base
    git archive b58fe37bf28338feb25df0a04fc5fb6c968eefe4 src | tar -x -C /tmp/issue78-base
    PYTHONPATH=/tmp/issue78-base/src:. python -m tests.test_issue78_load_baseline \
        tests/fixtures/issue78-load-baseline.json.gz

The saved report is complete, including all 40 requests. Only run_location is
normalized to identify the controlled mock environment rather than the host.
Gzip has a fixed mtime; measured request timestamps and durations remain intact.
"""

import asyncio
import gzip
import json
from pathlib import Path

import httpx
import pytest

from giraffe import runner
from giraffe.models import RunConfig, RunReport
from giraffe.reporting import _same_load_workload, _settings_reasons, compare_baseline
from tests.virtual_clock import run_virtual


BASE_REPORT = Path(__file__).parent / "fixtures" / "issue78-load-baseline.json.gz"
BASE_COMMIT = "b58fe37bf28338feb25df0a04fc5fb6c968eefe4"


async def generate_load_report(monkeypatch, *, restart=False):
    config = RunConfig.model_validate({
        "targets": [{"name": "local", "url": "http://unused.test/v1", "model": "test"}],
        "checks": ["load"], "stream": False, "max_memory_mb": 0,
        "restart_target": "local" if restart else None,
        "load_test": {
            "profile": {"short": {"input_tokens": 64, "output_tokens": 8}},
            "workloads": {"combinations": {"selected": ["short_short"]}},
            "patterns": {"concurrent": {"enabled": False}, "constant": {"enabled": True},
                         "rates": [40]},
            "schedule": {"duration_seconds": 1, "finish_seconds": .1},
            "pass_criteria": {"min_valid_responses": 1, "max_completion_ms": 1000},
        },
    })
    payloads = []

    if restart:
        async def completed_hook(run, target):
            run.observations[target.name]["restart"] = {
                "status": "hook_completed", "started_monotonic": asyncio.get_running_loop().time()}
            return True

        monkeypatch.setattr(runner, "_restart", completed_hook)

    async def handler(request):
        if request.method != "POST":
            return httpx.Response(200 if restart else 404)
        payload = json.loads(request.content)
        payloads.append(payload)
        await asyncio.sleep(.002)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "K7P4"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 64, "completion_tokens": 4}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    report = await runner.run_suite(config)
    assert report.abort_reason is None and not report.limits_reached
    assert len(payloads) == len(report.requests) == 40 + int(restart)
    assert {payload["max_tokens"] for payload in payloads} == ({8, config.max_output_tokens} if restart else {8})
    assert sum(record.scenario.startswith("load_") for record in report.requests) == 40
    assert sum(record.scenario == "readiness" for record in report.requests) == int(restart)
    # Both sides use the same in-memory endpoint and tokenizer; the machine
    # running this regression is not an experimental difference.
    report.manifest["run_location"] = {"hostname": "fixture", "system": "fixture",
                                       "machine": "fixture", "python": "3.12.1"}
    return report


@pytest.mark.usefixtures("local_tokenizer")
def test_generated_load_compares_with_saved_pre_output_budget_report(monkeypatch, tmp_path):
    current = run_virtual(monkeypatch, generate_load_report(monkeypatch))
    saved = tmp_path / "report.json"
    saved.write_text(current.model_dump_json())
    current = RunReport.model_validate_json(saved.read_text())
    baseline = RunReport.model_validate_json(gzip.decompress(BASE_REPORT.read_bytes()))
    assert baseline.manifest["test_fixture_provenance"]["commit"] == BASE_COMMIT
    assert len(baseline.requests) == 40
    assert {row.requested_max_tokens for row in baseline.requests} == {8}
    assert all(row.scenario.startswith("load_") for row in baseline.requests)
    assert current.manifest["fixture_pack"]["builtin_sha256"] != baseline.manifest["fixture_pack"]["builtin_sha256"]
    assert _same_load_workload(current.manifest, baseline.manifest)
    assert _settings_reasons(current, baseline) == []
    assert _settings_reasons(baseline, current) == []
    compared = compare_baseline(current, baseline)
    assert compared.baseline["targets"][0]["reasons"] == []
    assert compared.baseline["targets"][0]["load_test"]["points"]

    # The unused built-in digest exception requires exact Load source/settings
    # attestation and no companion check that might consume those fixtures.
    for change in ("profile", "unknown_source", "missing_identity", "mixed_builtin"):
        changed = baseline.model_copy(deep=True)
        if change == "profile":
            changed.manifest["config"]["load_test"]["profile"]["short"]["output_tokens"] += 1
        elif change == "unknown_source":
            changed.manifest["fixture_pack"]["workload_fixture_sha256"] = "unknown"
        elif change == "missing_identity":
            del changed.manifest["fixture_pack"]["workload_fixture_sha256"]
        else:
            changed.manifest["config"]["checks"].append("serving")
        assert "Fixture pack identity differs." in _settings_reasons(current, changed), change
        assert "Fixture pack identity differs." in _settings_reasons(changed, current), change


def test_output_budgets_generator_constant_is_the_current_workloads_source_hash():
    import hashlib

    from giraffe import workloads
    from giraffe.reporting import _OUTPUT_BUDGETS_GENERATOR_SOURCE

    assert _OUTPUT_BUDGETS_GENERATOR_SOURCE == hashlib.sha256(Path(workloads.__file__).read_bytes()).hexdigest()
    # The predecessor generator (BASE_COMMIT) hashed differently; see the generation script below.
    assert _OUTPUT_BUDGETS_GENERATOR_SOURCE != "2c5cfa60063bd236302582d25e7fe37e7807b829f487168cd09407d37bd60e6e"


@pytest.mark.usefixtures("local_tokenizer")
def test_load_run_from_another_generator_source_is_not_comparable(monkeypatch):
    current = run_virtual(monkeypatch, generate_load_report(monkeypatch))
    baseline = RunReport.model_validate_json(gzip.decompress(BASE_REPORT.read_bytes()))
    assert _same_load_workload(current.manifest, baseline.manifest)
    current.manifest["fixture_pack"]["workload_fixture_sha256"] = "0" * 64  # an unattested generator hash
    assert not _same_load_workload(current.manifest, baseline.manifest)
    assert "Fixture pack identity differs." in _settings_reasons(current, baseline)


@pytest.mark.usefixtures("local_tokenizer")
def test_load_restart_still_requires_matching_readiness_fixture(monkeypatch):
    current = run_virtual(monkeypatch, generate_load_report(monkeypatch, restart=True))
    baseline = current.model_copy(deep=True)
    saved_base = RunReport.model_validate_json(gzip.decompress(BASE_REPORT.read_bytes()))
    baseline.manifest["fixture_pack"]["builtin_sha256"] = saved_base.manifest["fixture_pack"]["builtin_sha256"]
    assert _same_load_workload(current.manifest, baseline.manifest)
    assert "Fixture pack identity differs." in _settings_reasons(current, baseline)
    assert "Fixture pack identity differs." in _settings_reasons(baseline, current)


if __name__ == "__main__":
    import sys
    import tempfile

    from giraffe import workloads
    from tests.conftest import local_tokenizer

    # Fail rather than label a report from another generator as the base.
    assert workloads.FIXTURE_SOURCE_SHA256 == "2c5cfa60063bd236302582d25e7fe37e7807b829f487168cd09407d37bd60e6e"
    with tempfile.TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as patch:
        local_tokenizer.__wrapped__(patch, Path(temporary))
        baseline = asyncio.run(generate_load_report(patch))
    baseline.manifest["test_fixture_provenance"] = {
        "commit": BASE_COMMIT, "transport": "httpx.MockTransport",
        "tokenizer": "tests.conftest.local_tokenizer"}
    encoded = (json.dumps(baseline.model_dump(mode="json", exclude_defaults=True), indent=2) + "\n").encode()
    Path(sys.argv[1]).write_bytes(gzip.compress(encoded, mtime=0))
