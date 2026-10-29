"""Socket integration coverage for the orchestrator outside the socket sandbox."""

import json

from giraffe.cli import main
from giraffe.models import DEFAULT_CHECKS
from tests.fake_endpoint import FakeEndpoint


def test_url_only_defaults_pass_a_healthy_single_slot_server(tmp_path, capsys):
    directory = tmp_path / "run"
    with FakeEndpoint(service_slots=1) as endpoint:
        assert main(["run", "--url", endpoint.url, "--html", "--output", str(directory)]) == 0
    output = capsys.readouterr()
    assert "Using model fake-model (only model served)" in output.err
    assert output.out.startswith("PASS  4 checks passed")
    assert "Not selected:" in output.out
    assert "RAM" not in output.out
    report = json.loads((directory / "report.json").read_text())
    assert report["overall"] == "pass"
    assert report["manifest"]["config"]["checks"] == list(DEFAULT_CHECKS)
    assert (directory / "report.html").is_file()
