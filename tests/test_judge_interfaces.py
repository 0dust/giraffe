"""Judge configuration stays portable across human and agent entry points."""

import io
import json

import pytest

from giraffe.cli import main
from giraffe.handoff import cli_command
from giraffe.models import ConfigDraft, RunConfig
from giraffe.request_pack import pack_identity
from giraffe.reporting import _settings_reasons, compare_baseline
from tests.test_handoff import run_command
from tests.test_reporting import make_report
from tests.test_web import browser


def judged_config():
    return {
        "targets": [{"name": "local", "url": "http://localhost:8000/v1", "model": "subject"}],
        "checks": ["correctness", "load"],
        "load_test": {"patterns": {"concurrent": {"enabled": False}, "constant": {"enabled": True}}},
        "request_source": "request_pack",
        "request_pack": {"version": 1, "cases": [{
            "fixture_id": "refund", "messages": [{"role": "user", "content": "What is the return window?"}],
            "scorer": "judge", "expected": "Returns are accepted within 30 days.",
            "rubric": "The number of days must be correct.",
        }]},
        "judging": {"judges": [{"name": "reviewer", "url": "http://localhost:9000/v1",
                                "model": "judge-model", "api_key_env": "JUDGE_API_KEY"}]},
    }


async def test_judge_draft_preview_cli_and_export_agree_without_model_calls(tmp_path, monkeypatch, capsys):
    values = judged_config()
    values["targets"][0]["model"] = ""
    values["judging"]["judges"][0]["model"] = ""
    monkeypatch.setattr("giraffe.client.LLMClient.__aenter__", lambda *a: pytest.fail("Preview called a model"))
    async with browser(tmp_path) as client:
        preview = await client.post("/api/config/preview", json=values)
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["request_pack_preview"]["judging"]["ready"] is False
        assert body["request_pack_preview"]["judging"]["planned_call_upper_bound"] > 0
        draft = await client.post("/api/config/draft", json=values)
        assert draft.status_code == 200
        for path in ("/api/config/cli", "/api/runs"):
            invalid = await client.post(path, json={"config": values})
            assert invalid.status_code == 422
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(values)))
    assert main(["preview", "--config", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["config"] == body["config"]
    assert ConfigDraft.model_validate(body["config"]).judging.judges[0].api_key_env == "JUDGE_API_KEY"


def test_judge_copy_cli_embeds_golden_answer_and_complete_panel(tmp_path):
    config = RunConfig.model_validate(judged_config())
    config.judging.judges.append(config.judging.judges[0].model_copy(update={"name": "second", "model": "another"}))
    result, captured, _ = run_command(tmp_path, cli_command(config))
    assert result.returncode == 0, result.stderr
    restored = RunConfig.model_validate_json(captured["stdin"])
    assert restored == config
    assert restored.request_pack.cases[0].expected == "Returns are accepted within 30 days."


@pytest.mark.parametrize("change", ["model", "rubric", "reference"])
def test_judge_change_invalidates_upgrade_comparison(change):
    config = RunConfig.model_validate(judged_config())
    old, current = make_report("old"), make_report()
    for report in (old, current):
        report.manifest["config"] = config.model_dump(mode="json")
        report.manifest["request_pack"] = pack_identity(config)
    if change == "model":
        config.judging.judges[0].model = "different-judge"
    elif change == "rubric":
        config.request_pack.cases[0].rubric = "Require exactly 30 days and no exceptions."
    else:
        config.request_pack.cases[0].expected = "Returns are accepted within 60 days."
    current.manifest["config"] = config.model_dump(mode="json")
    current.manifest["request_pack"] = pack_identity(config)
    assert "Request pack identity differs." in _settings_reasons(current, old)


@pytest.mark.parametrize("incomplete_run", ["Current", "Baseline"])
@pytest.mark.parametrize("loaded", [False, True])
def test_missing_judgements_prevent_upgrade_claims(incomplete_run, loaded):
    config = RunConfig.model_validate(judged_config())
    old, current = make_report("old"), make_report()
    for report in (old, current):
        report.manifest["config"] = config.model_dump(mode="json")
        report.manifest["request_pack"] = pack_identity(config)
        for record in report.requests:
            record.fixture_id = "pack:refund"
        if loaded:
            report.requests[-1].traffic_stage = 0
    incomplete = current if incomplete_run == "Current" else old
    incomplete.requests[-1].score = None
    result = compare_baseline(current, old)
    assert result.baseline["status"] == "inconclusive"
    target = result.baseline["targets"][0]
    assert f"{incomplete_run} AI judge results are incomplete; correctness and capacity cannot be compared." in target["reasons"]
    assert target["comparisons"] == []


def test_intentionally_unscored_pack_does_not_require_judgements():
    config = RunConfig.model_validate(judged_config())
    config.request_pack.cases[0].scorer = "none"
    old, current = make_report("old"), make_report()
    for report in (old, current):
        report.manifest["config"] = config.model_dump(mode="json")
        report.manifest["request_pack"] = pack_identity(config)
        report.requests[-1].fixture_id = "pack:refund"
        report.requests[-1].score = None
    assert not any("AI judge" in reason for reason in _settings_reasons(current, old))
