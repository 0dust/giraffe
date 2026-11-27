"""Pin generated request plans, independently of module source formatting."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from giraffe import concurrent_load, load_test, multi_user, runner, session_sampling, workloads
from giraffe.deployment import fixture_hash
from giraffe.fixtures import builtin_fixtures
from giraffe.logprob_workload import logprob_fixtures
from giraffe.models import LoadTest, LoadTestSettings, MultiUser, RunConfig, ScaleDownConfig, Sessions
from giraffe.scale_down import resolve_fixtures
from giraffe.workload_versions import WORKLOAD_VERSIONS


NONCE = "0" * 32
PINNED = {
    "generator": (1, "1327c00ca81d862e3685dc3f2aa49c941a849c5cbc20ae7e578192d1766c4253"),
    "fixed_sessions": (1, "84e2056aa1e6fb8068c2848480478dfdc4e39f41e1e66512d2d37a4a02815b1d"),
    "session_sampling": (1, "54c8029050f0f728e9d5a78f03c06d786c533d5154119bfb9855489dd5d130bc"),
    "multi_user": (1, "5f1f0f51dd70726696e20ce9f033c15bd2fdabc40fb97406ca6009769d987516"),
    "scale_down": (1, "3403a497536a5c37c6a28f46c7ec3a6e09c71b20ee11bec196c93e5497041541"),
    "load_test": (1, "4ddb40c97866d0db11ab68208d900f59146bc441f6d05e78ffa269f334a0840d"),
    "logprob": (1, "6d8fa2a11aad42d276707e5289b68a49e09cfdaf15dd9223e9571394bf9eec18"),
}
# No pure request builder exists for these workloads: their RequestSpecs are
# constructed inside asynchronous execution, with run/response/credential state.
VERSION_ONLY = {"gateway_access": 1, "gateway_policy": 1, "tool_conversations": 1}


def _config(**settings):
    return RunConfig(targets=[{"name": "service", "url": "http://localhost:8000/v1", "model": "test"}],
                     **settings)


def _requests(specs):
    # Scoring expectations, IDs and reporting metadata never reach the server.
    fields = {"messages", "max_tokens", "stream", "options", "cancel_after_ms",
              "cancel_after_chunks", "request_timeout_seconds", "identity",
              "requested_input_tokens", "requested_output_tokens"}
    return [spec.model_dump(mode="json", include=fields) for spec in specs]


def _generator():
    requests = {name: _requests(specs) for name, specs in builtin_fixtures(_config()).items()}
    prefix = runner._check_config(_config(checks=["prefix"]), "prefix")
    consistency = runner._check_config(_config(
        checks=["consistency"],
        consistency={"prompts_per_shape": len(workloads.COPY_PASSAGES) + 1,
                     "shapes": ["short", "long"]},
    ), "consistency")
    requests["prefix"] = _requests(workloads.prefix_requests(prefix, nonce=lambda: NONCE))
    requests["consistency"] = [_requests([spec]) for _, _, spec in workloads.consistency_requests(consistency)]
    tools = runner._check_config(_config(checks=["tools"], forced_tool_diagnostic=True), "tools")
    requests["tools"] = _requests([
        workloads.tool_request(tools, fixture, mode, forced=forced)
        for forced in (False, True)
        for fixture in workloads.resolved_tool_fixtures(tools)
        for mode in workloads.resolved_tool_modes(tools)
    ])
    return requests


def _fixed_sessions():
    config = _config(checks=["sessions"])
    settings = config.sessions
    requests = []
    for mode in settings.modes:
        for index in range(settings.sessions):
            value = f"Session_{index:04d}_{mode}"
            history = []
            for turn, prompt in enumerate(settings.turns):
                prompt = workloads.session_prompt(prompt, turn, value)
                messages = [*history, {"role": "user", "content": prompt}]
                request = workloads.spec(
                    prompt, f"session_{mode}_turn{turn}", "sessions",
                    max_tokens=settings.max_tokens,
                    request_timeout_seconds=config.request_timeout_seconds,
                    options=workloads.deterministic_options(config),
                )
                request.messages = messages
                requests.append(request)
                # A deterministic valid reply makes live history reproducible too.
                history = [*messages, {"role": "assistant", "content": value}]
    return _requests(requests)


def _load_test():
    settings = LoadTest()
    points = load_test.test_points(settings)
    return {
        "prompts": [concurrent_load.prompt(chars, variant, nonce=NONCE)
                    for chars in (0, 400) for variant in ("short_answer", "long_output")],
        "points": points,
        "specs": [concurrent_load.make_spec(settings, shape, "version_guard",
                                            next(point for point in points if point["shape"] == shape), 400,
                                            nonce=NONCE).model_dump(mode="json")
                  for shape in load_test.running_shapes(settings)],
    }


def _multi_user():
    settings = MultiUser()
    config = _config()
    population = multi_user.population_plan(settings)
    load_settings = LoadTestSettings(profile=settings.profile,
                                     request_timeout_seconds=config.request_timeout_seconds)
    first = population[0]
    shape = first["profile"]
    size, output = shape.split("_")
    calibration = {(size, "short_answer" if output == "short" else "long_output"): {"filler_chars": 400}}
    # The public builder is also used by run_window for each persistent user.
    specs = []
    for level in settings.levels:
        point = {"id": f"multi_user_{level}_measurement", "type": "combinations", "workload": "combinations",
                 "pattern": "concurrent", "level": level, "rate": None, "bursts": False, "source": "primary"}
        specs.append(load_test.build_spec(SimpleNamespace(config=config), load_settings, point, shape,
                                         calibration, nonce=NONCE))
    return {"population": population, "first_user_requests": _requests(specs)}


def _session_sampling():
    settings = Sessions(sampling={"think_time_seconds": {"values": [0, .25], "weights": [1, 1]}})
    return session_sampling.session_plan(settings)


def _scale_down():
    return _requests(resolve_fixtures(ScaleDownConfig(target="service"), builtin_fixtures(_config())))


def _logprob():
    config = _config()
    return {name: _requests([spec]) for name, spec in logprob_fixtures(config, builtin_fixtures(config)).items()}


BUILDERS = {"generator": _generator, "fixed_sessions": _fixed_sessions, "session_sampling": _session_sampling, "multi_user": _multi_user,
            "scale_down": _scale_down, "load_test": _load_test, "logprob": _logprob}


def _failure(name):
    return (f"The {name} workload's generated requests changed. If a server would see different requests, "
            f"bump WORKLOAD_VERSIONS['{name}'] in src/giraffe/workload_versions.py, then update PINNED here. "
            "If they did not change, the digest inputs are too broad.")


@pytest.mark.parametrize("name", PINNED)
def test_generated_requests_match_workload_version(name):
    digest = hashlib.sha256(json.dumps(BUILDERS[name](), sort_keys=True).encode()).hexdigest()
    assert WORKLOAD_VERSIONS[name] == PINNED[name][0], _failure(name)
    assert digest == PINNED[name][1], _failure(name)


@pytest.mark.parametrize("name", VERSION_ONLY)
def test_workload_without_pure_request_builder_has_explicit_version(name):
    assert WORKLOAD_VERSIONS[name] == VERSION_ONLY[name], _failure(name)


def test_all_workloads_have_guards():
    assert set(WORKLOAD_VERSIONS) == set(PINNED) | set(VERSION_ONLY)


def test_module_comments_do_not_affect_workload_fingerprint(monkeypatch):
    from giraffe.runner import build_workload_identity

    config = _config(checks=["load"])
    identity, shared = build_workload_identity(config)
    before = fixture_hash(identity)

    def no_module_bytes(*args, **kwargs):
        raise AssertionError("Workload identity must not read module source bytes")

    monkeypatch.setattr(Path, "read_bytes", no_module_bytes)
    after_identity, after_shared = build_workload_identity(config)
    assert fixture_hash(after_identity) == before
    assert after_shared == shared


@pytest.mark.parametrize(("checks", "settings", "names"), [
    (["serving"], {}, set()),
    (["load"], {}, {"load_test"}),
    (["multi_user"], {}, {"multi_user"}),
    (["sessions"], {}, {"generator", "fixed_sessions"}),
    (["sessions"], {"sessions": {"sampling": {}}}, {"generator", "fixed_sessions", "session_sampling"}),
    (["consistency"], {}, {"generator"}),
    (["consistency"], {"consistency": {"logprobs": {}}}, {"generator", "logprob"}),
    (["gateway_access"], {}, {"gateway_access"}),
    (["gateway_access"], {"gateway_access": {}}, {"gateway_access"}),
    (["tools"], {}, {"generator"}),
    (["gateway_policy"], {}, {"gateway_policy"}),
    (["scale_down"], {"scale_down": {"target": "service"}}, {"scale_down"}),
])
def test_identity_versions_follow_selected_workloads(checks, settings, names):
    from giraffe.runner import build_workload_identity

    identity, _ = build_workload_identity(_config(checks=checks, **settings))
    assert identity["workload_versions"] == {name: WORKLOAD_VERSIONS[name] for name in names}
    if "multi_user" in checks:
        assert identity["multi_user_window_policy"] == "completed-output-v1"


def test_unselected_settings_do_not_add_workload_versions():
    from giraffe.runner import build_workload_identity

    config = _config(checks=["serving"], sessions={"sampling": {}}, multi_user={},
                     consistency={"logprobs": {}}, gateway_access={})
    identity, _ = build_workload_identity(config)
    assert identity["workload_versions"] == {}


def test_tool_conversation_version_requires_configured_conversations():
    from giraffe.models import ToolChecks, ToolConversationFixture
    from giraffe.runner import build_workload_identity
    from giraffe.tool_checks import default_tool_fixture

    default = default_tool_fixture()
    fixture = ToolConversationFixture(
        fixture_id="weather-conversation", messages=default.messages, tools=default.tools,
        steps=[{"expected_calls": [{"name": "get_weather", "arguments": {"city": "Delhi"},
                                     "result": {"observation_id": "{{observation_id}}"}}]}],
        final_content=[{"mode": "contains", "value": "{{observation_id}}"}],
    )
    config = _config(checks=["tools"], tool_checks=ToolChecks(conversations=[fixture]))
    identity, _ = build_workload_identity(config)
    assert identity["workload_versions"] == {"generator": WORKLOAD_VERSIONS["generator"],
                                              "tool_conversations": WORKLOAD_VERSIONS["tool_conversations"]}
    assert identity["resolved_tool_checks"]["conversations"] == [fixture.model_dump(mode="json")]


@pytest.mark.parametrize("shared_checks", [["serving"], ["load"]])
def test_shared_identity_removes_gateway_versions_and_recomputes_generator(shared_checks, monkeypatch):
    from giraffe.runner import build_workload_identity

    base, _ = build_workload_identity(_config(checks=shared_checks))
    config = _config(checks=[*shared_checks, "gateway_access"], gateway_access={})
    full, shared = build_workload_identity(config)
    assert "generator" not in full["workload_versions"]
    assert shared["shared_workload_fixture_sha256"] == fixture_hash(base)
    monkeypatch.setitem(WORKLOAD_VERSIONS, "gateway_access", 2)
    changed, changed_shared = build_workload_identity(config)
    assert fixture_hash(changed) != fixture_hash(full)
    assert changed_shared == shared
    monkeypatch.setitem(WORKLOAD_VERSIONS, "generator", 2)
    generator_changed, generator_shared = build_workload_identity(config)
    assert fixture_hash(generator_changed) == fixture_hash(changed)
    assert generator_shared == changed_shared


def test_workload_version_bump_changes_selected_identity(monkeypatch):
    from giraffe.runner import build_workload_identity

    config = _config(checks=["load"])
    before, _ = build_workload_identity(config)
    monkeypatch.setitem(WORKLOAD_VERSIONS, "load_test", 2)
    after, _ = build_workload_identity(config)
    assert fixture_hash(before) != fixture_hash(after)


def test_gateway_settings_preserve_shared_comparability_but_generator_version_does_not(monkeypatch):
    from giraffe.reporting import _settings_reasons
    from giraffe.runner import build_workload_identity
    from tests.test_reporting import make_report

    def report(profile):
        config = _config(checks=["serving", "sessions", "gateway_access"],
                         gateway_access={"profile": profile})
        result = make_report()
        result.manifest["config"] = config.model_dump(mode="json")
        identity, shared = build_workload_identity(config)
        result.manifest["fixture_pack"].update(workload_fixture_sha256=fixture_hash(identity), **shared)
        return result

    baseline = report("litellm")
    current = report("other-gateway")
    reason = "Fixture pack identity differs."
    assert reason not in _settings_reasons(current, baseline)
    assert reason not in _settings_reasons(baseline, current)

    monkeypatch.setitem(WORKLOAD_VERSIONS, "generator", 2)
    changed = report("other-gateway")
    assert reason in _settings_reasons(changed, baseline)
    assert reason in _settings_reasons(baseline, changed)


def test_fixed_session_guard_covers_prompt_changes(monkeypatch):
    before = hashlib.sha256(json.dumps(_fixed_sessions(), sort_keys=True).encode()).hexdigest()
    original = workloads.session_prompt
    monkeypatch.setattr(workloads, "session_prompt",
                        lambda *args: original(*args).replace("bakery name", "bakery code"))
    after = hashlib.sha256(json.dumps(_fixed_sessions(), sort_keys=True).encode()).hexdigest()
    assert before != after


@pytest.mark.parametrize("checks", [["load"], ["load", "serving", "first_output", "generation", "context"]])
def test_generator_bump_does_not_change_load_identity(checks, monkeypatch):
    from giraffe.runner import build_workload_identity

    config = _config(checks=checks)
    before, _ = build_workload_identity(config)
    monkeypatch.setitem(WORKLOAD_VERSIONS, "generator", 2)
    after, _ = build_workload_identity(config)
    assert fixture_hash(before) == fixture_hash(after)


@pytest.mark.parametrize(("checks", "settings", "changes"), [
    (["sessions"], {}, True),
    (["load"], {}, False),
    (["multi_user"], {"multi_user": {"conversations": {}}}, True),
    (["sessions"], {"sessions": {"workloads": [{"name": "unused", "turns": ["Hi", "More"]}]}}, True),
    (["multi_user"], {"multi_user": {"conversations": {
        "workloads": [{"name": "unused", "turns": ["Hi", "More"]}]}}}, True),
    (["sessions"], {"sessions": {"profile": "performance", "modes": ["live"],
                                 "workloads": [{"name": "chat", "turns": ["Hi", "More"]}]}}, False),
])
def test_fixed_sessions_version_follows_prompt_consumers(checks, settings, changes, monkeypatch):
    from giraffe.runner import build_workload_identity

    config = _config(checks=checks, **settings)
    before, _ = build_workload_identity(config)
    monkeypatch.setitem(WORKLOAD_VERSIONS, "fixed_sessions", 2)
    after, _ = build_workload_identity(config)
    assert (fixture_hash(before) != fixture_hash(after)) is changes


@pytest.mark.parametrize("sampling", [None, {"think_time_seconds": {"values": [0, .25]}}])
def test_multi_user_session_sampling_version_follows_sampling(sampling, monkeypatch):
    from giraffe.runner import build_workload_identity

    config = _config(checks=["multi_user"], multi_user={"conversations": {"sampling": sampling}})
    before, _ = build_workload_identity(config)
    monkeypatch.setitem(WORKLOAD_VERSIONS, "session_sampling", 2)
    after, _ = build_workload_identity(config)
    assert (fixture_hash(before) != fixture_hash(after)) is (sampling is not None)


def test_generator_guard_covers_second_copy_passage(monkeypatch):
    before = _generator()
    passages = list(workloads.COPY_PASSAGES)
    passages[1] = passages[1].replace("brass key", "glass key")
    monkeypatch.setattr(workloads, "COPY_PASSAGES", tuple(passages))
    assert fixture_hash(_generator()) != fixture_hash(before)


def test_generator_guard_covers_same_length_label_change(monkeypatch):
    before = _generator()
    monkeypatch.setattr(workloads, "LABEL_INSTRUCTION", workloads.LABEL_INSTRUCTION.replace("K7P4", "K7P5"))
    assert fixture_hash(_generator()) != fixture_hash(before)


class CaptureRun:
    def __init__(self, config):
        self.config = config
        self.records = []
        self.requests = []
        self.unfinished = {config.targets[0].name: set()}
        self.observations = {config.targets[0].name: {}}
        self.planned = []
        self.finished = []

    def allowed(self, target):
        return True

    def plan_condition(self, target, scenario, **kwargs):
        self.planned.append(scenario)

    def start_condition(self, target, scenario, *args, **kwargs):
        self.planned.append(scenario)

    def finish_condition(self, target, scenario, *args, **kwargs):
        self.finished.append(scenario)

    async def request(self, target, client, request, *, on_start=None):
        from giraffe.models import RequestRecord

        if on_start:
            on_start()
        self.requests.append(request.model_copy(deep=True))
        record = RequestRecord(id=str(len(self.requests)), target=target.name,
                               fixture_id=request.fixture_id, scenario=request.scenario,
                               check_ids=request.check_ids, started_at="", status="completed",
                               valid=True, output="K7P4")
        self.records.append(record)
        return record


async def test_prefix_builder_matches_sent_requests(monkeypatch):
    config = _config(checks=["prefix"], prefix={"prefix_chars": 128, "repeats": 3, "history_turns": 3},
                     test_options={"prefix": {"max_output_tokens": 48, "request_timeout_seconds": 7}})
    monkeypatch.setattr(workloads.uuid, "uuid4", lambda: SimpleNamespace(hex=NONCE))
    run = CaptureRun(config)
    await workloads.prefixes(run, config.targets[0], None)
    expected = workloads.prefix_requests(runner._check_config(config, "prefix"), nonce=lambda: NONCE)
    assert len(run.requests) == 9
    assert _requests(run.requests) == _requests(expected)
    # Admission and reporting metadata must survive extraction too.
    assert run.requests == expected
    assert run.planned == run.finished == ["prefix_shared", "prefix_independent", "prefix_history"]
    history = run.requests[-3:]
    assert [len(request.messages) for request in history] == [1, 3, 5]
    assert history[-1].messages[1] == {"role": "assistant", "content": "K7P4"}


async def test_consistency_builder_matches_sent_requests():
    config = _config(checks=["consistency"], consistency={
        "shapes": ["short", "long"], "prompts_per_shape": len(workloads.COPY_PASSAGES) + 1,
        "levels": [2], "repetitions": 2, "temperature": .25, "seed": 17,
    }, test_options={"consistency": {"max_output_tokens": 80, "request_timeout_seconds": 7}})
    run = CaptureRun(config)
    await workloads.consistency(run, config.targets[0], None, {})
    expected = workloads.consistency_requests(runner._check_config(config, "consistency"))
    for level in (1, 2):
        first_per_prompt = {}
        for request in run.requests:
            if request.workload["level"] == level:
                key = (request.workload["input_bucket"], request.workload["prompt_index"])
                first_per_prompt.setdefault(key, request)
        assert list(first_per_prompt) == [(shape, index) for shape, index, _ in expected]
        assert _requests(first_per_prompt.values()) == _requests([spec for _, _, spec in expected])
    assert run.planned == run.finished


async def test_tool_builder_matches_sent_requests():
    config = _config(checks=["tools"], forced_tool_diagnostic=True)
    run = CaptureRun(config)
    await workloads.tools(run, config.targets[0], None)
    effective = runner._check_config(config, "tools")
    expected = [workloads.tool_request(effective, fixture, mode, forced=forced)
                for forced in (False, True)
                for fixture in workloads.resolved_tool_fixtures(effective)
                for mode in workloads.resolved_tool_modes(effective)]
    assert len(run.requests) == 4
    assert _requests(run.requests) == _requests(expected)
    assert run.planned == run.finished
