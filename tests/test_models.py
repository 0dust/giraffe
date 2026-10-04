import pytest
from pydantic import ValidationError

from giraffe.models import CheckResult, RunConfig, Target, overall_status


def test_bounds_and_target_names_are_checked_before_traffic():
    target = Target(name="local", url="http://localhost:11434/v1", model="example")
    for override in ({"concurrency": 0}, {"max_requests": 0}, {"context_limit": 0}):
        with pytest.raises(ValidationError):
            RunConfig(targets=[target], **override)
    with pytest.raises(ValidationError, match="unique"):
        RunConfig(targets=[target, target])


def test_missing_required_evidence_cannot_pass():
    result = CheckResult(id="serving", target="local", title="Serving", status="inconclusive", summary="No samples")
    assert overall_status([result]) == "inconclusive"
    assert overall_status([]) == "inconclusive"
    assert overall_status([result.model_copy(update={"status": "pass"})], "interrupted") == "inconclusive"


def test_optional_missing_telemetry_does_not_block_api_checks():
    serving = CheckResult(id="serving", target="local", title="Serving", status="pass", summary="Valid completion")
    gpu = CheckResult(id="gpu", target="local", title="GPU", status="inconclusive", required=False, summary="No metrics")
    assert overall_status([serving, gpu]) == "pass"


def test_request_overrides_cannot_escape_token_or_concurrency_budget():
    with pytest.raises(ValidationError, match="request_options"):
        RunConfig(targets=[Target(name="local", url="http://localhost", model="example")], request_options={"n": 100})
