"""Socket integration coverage for default CLI selection, scoring and reports."""

import json

import pytest

from giraffe import runner
from giraffe.cli import main
from giraffe.models import DEFAULT_CHECKS
from giraffe.transport import HTTPClientPool
from tests.fake_endpoint import FakeEndpoint
from tests.measurement_clock import MeasurementClock


@pytest.mark.parametrize("condition, verdict, exit_code", [
    ("healthy", "pass", 0), ("wrong_answer", "fail", 1),
    ("client_delay", "inconclusive", 2), ("endpoint_delay", "fail", 1),
])
def test_url_only_defaults_assess_a_single_slot_server(
    tmp_path, capsys, monkeypatch, condition, verdict, exit_code,
):
    # This covers defaults and real socket responses, not the scheduler load of
    # the machine running pytest. Timing qualification retains its own control.
    clock = MeasurementClock(monkeypatch)
    execute = runner.LLMClient.execute
    send = HTTPClientPool.send

    async def measured_send(self, request, **kwargs):
        response = await send(self, request, **kwargs)
        if condition == "endpoint_delay" and request.method == "POST":
            # Slow endpoint, responsive client: this must remain an attributable
            # timing failure under the unchanged ten-second default limit.
            clock.advance(11)
        return response

    monkeypatch.setattr(HTTPClientPool, "send", measured_send)

    async def measured(self, spec, **kwargs):
        record = await execute(self, spec, **kwargs)
        if condition == "client_delay" and spec.scenario == "warm":
            clock.advance(.3, responsive=False)
            clock.advance(.001)
        return record

    monkeypatch.setattr(runner.LLMClient, "execute", measured)
    directory = tmp_path / "run"
    with FakeEndpoint(mode="wrong" if condition == "wrong_answer" else "healthy", service_slots=1) as endpoint:
        assert main(["run", "--url", endpoint.url, "--html", "--output", str(directory)]) == exit_code
    output = capsys.readouterr()
    assert "Using model fake-model (only model served)" in output.err
    assert output.out.startswith(verdict.upper())
    assert "Not selected:" in output.out
    assert "RAM" not in output.out
    report = json.loads((directory / "report.json").read_text())
    assert report["overall"] == verdict
    assert report["manifest"]["config"]["checks"] == list(DEFAULT_CHECKS)
    checks = {check["id"]: check for check in report["checks"]}
    if condition == "healthy":
        assert output.out.startswith("PASS  4 checks passed")
        assert all(checks[name]["status"] == "pass" for name in DEFAULT_CHECKS)
    elif condition == "wrong_answer":
        assert checks["correctness"]["status"] == "fail"
    elif condition == "endpoint_delay":
        assert checks["first_output"]["status"] == "fail"
        assert checks["first_output"]["metrics"]["first_output_p95_ms"] > 10000
        assert checks["first_output"]["metrics"]["timing_quality"]["status"] == "qualified"
    else:
        assert checks["first_output"]["status"] == "inconclusive"
        assert checks["first_output"]["metrics"]["reason_code"] == "client_delayed"
        assert all(checks[name]["status"] == "pass" for name in ("access", "serving", "correctness"))
    assert (directory / "report.html").is_file()
