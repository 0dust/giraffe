"""Explain confirmed optional capability rejections in saved request evidence."""

import json

import pytest

from tests.test_ux_results import run_render_checks


@pytest.mark.parametrize(
    ("overrides", "assertions"),
    [
        pytest.param(
            {},
            r"""
assert.match(html,/Unsupported optional capability: the endpoint rejected <code>logprobs<\/code> \(HTTP 400\)/);
assert.match(html,/per-token timing stays unavailable/);
assert.match(html,/class="notice[^"]*"[^>]*>HTTP 400<\/div>/);
assert.doesNotMatch(html,/class="notice error"/);
""",
            id="confirmed-logprobs-rejection",
        ),
        pytest.param(
            {
                "http_status": 422,
                "error": "HTTP 422",
                "workload": {
                    "performance_evidence_probe": True,
                    "rejected_evidence_field": "stream_options",
                },
            },
            r"""
assert.match(html,/Unsupported optional capability: the endpoint rejected <code>stream_options<\/code> \(HTTP 422\)/);
assert.match(html,/streamed token usage stays unavailable/);
assert.match(html,/class="notice[^"]*"[^>]*>HTTP 422<\/div>/);
assert.doesNotMatch(html,/class="notice error"/);
""",
            id="confirmed-stream-options-rejection",
        ),
        pytest.param(
            {"workload": {"performance_evidence_probe": True}},
            r"""
assert.doesNotMatch(html,/Unsupported optional capability/);
assert.match(html,/class="notice error"[^>]*>HTTP 400/);
""",
            id="unconfirmed-rejection",
        ),
        pytest.param(
            {"http_status": 500, "error": "HTTP 500"},
            r"""
assert.doesNotMatch(html,/Unsupported optional capability/);
assert.match(html,/class="notice error"[^>]*>HTTP 500/);
""",
            id="server-error",
        ),
        pytest.param(
            {"scenario": "initial"},
            r"""
assert.doesNotMatch(html,/Unsupported optional capability/);
assert.match(html,/class="notice error"[^>]*>HTTP 400/);
""",
            id="non-probe-scenario",
        ),
        pytest.param(
            {
                "workload": {
                    "performance_evidence_probe": True,
                    "rejected_evidence_field": "temperature",
                }
            },
            r"""
assert.doesNotMatch(html,/Unsupported optional capability/);
assert.match(html,/class="notice error"[^>]*>HTTP 400/);
""",
            id="unknown-field",
        ),
    ],
)
def test_optional_probe_rejection_notice(overrides, assertions):
    record = {
        "id": "probe",
        "target": "local",
        "scenario": "performance_evidence_probe",
        "status": "failed",
        "http_status": 400,
        "error": "HTTP 400",
        "workload": {
            "performance_evidence_probe": True,
            "rejected_evidence_field": "logprobs",
        },
        **overrides,
    }
    run_render_checks(
        "(async()=>{\nconst r=" + json.dumps(record) + r""";
state.detail={report:{manifest:{config:{retention:'all'}},requests:[r]}};
nodes['#request-dialog']={innerHTML:'',showModal(){this.shown=true;}};
await openRequest('probe');
const html=nodes['#request-dialog'].innerHTML;
assert(nodes['#request-dialog'].shown);
""" + assertions + "\n})();"
    )
