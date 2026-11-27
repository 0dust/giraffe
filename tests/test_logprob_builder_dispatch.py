"""The logprob guard builder returns the requests dispatched by the executor."""

import pytest

from giraffe import logprob_workload, runner
from giraffe.fixtures import builtin_fixtures
from tests.test_workload_versions import CaptureRun, _config, _requests


@pytest.mark.parametrize("response_mode,top_logprobs", [("complete", 0), ("stream", 3)])
async def test_logprob_builder_matches_sent_requests(response_mode, top_logprobs):
    config = _config(checks=["consistency"], consistency={
        "shapes": ["short", "long"], "levels": [2], "repetitions": 2,
        "temperature": .25, "seed": 17,
        "logprobs": {"response_mode": response_mode, "top_logprobs": top_logprobs,
                     "priming_rounds": 1},
    }, test_options={"consistency": {"max_output_tokens": 80, "request_timeout_seconds": 7}})
    run = CaptureRun(config)
    await logprob_workload.run_logprob_consistency(run, config.targets[0], None, {})
    effective = runner._check_config(config, "consistency")
    templates = logprob_workload.logprob_fixtures(effective, builtin_fixtures(effective))
    expected = [request
                for shape in effective.consistency.shapes
                for level in (1, 2)
                for phase, rounds in (("priming", 1), ("measured", 2))
                for repeat in range(rounds)
                for request in logprob_workload.logprob_requests(
                    effective, templates, shape, level, phase, repeat)]
    assert len(run.requests) == 24
    assert _requests(run.requests) == _requests(expected)
    assert run.requests == expected
    assert all(request.options["logprobs"] is True for request in run.requests)
    assert all(request.options["top_logprobs"] == top_logprobs for request in run.requests)
    assert all(request.stream == (response_mode == "stream") for request in run.requests)
    assert run.planned == run.finished
