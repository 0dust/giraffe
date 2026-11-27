"""Pin generated request plans, independently of module source formatting."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from giraffe import concurrent_load, load_test, logprob_workload, multi_user, runner, workloads
from giraffe.deployment import fixture_hash
from giraffe.fixtures import builtin_fixtures
from giraffe.models import SHAPES, LoadTest, LoadTestSettings, MultiUser, RunConfig, ScaleDownConfig
from giraffe.scale_down import resolve_fixtures
from giraffe.workload_versions import WORKLOAD_VERSIONS


NONCE = "0" * 32
PINNED = {
    "prefix": (1, "721d473c4755c1ffd6c23b4fd4606805923e034c79b5452f6108b1a6072d025a"),
    "consistency": (1, "e370b46d376d786cb54035d025c6400a8c701f66431d352ba5f1602c98e0856f"),
    "tools": (1, "a6cea9e42a2277e0ed896442835f247ffe991b0c0e73cf5c2bdc59cf0ff7001d"),
    "fixed_sessions": (1, "84e2056aa1e6fb8068c2848480478dfdc4e39f41e1e66512d2d37a4a02815b1d"),
    "session_plan": (1, "9d8e586754a51e7257d31ebe033b5f90f505b00719a127e249f587f74bc56c68"),
    "multi_user": (1, "6346d9c3563f03fa6e5b985eda335be879b1793a2c23f9a21489a800237209bc"),
    "scale_down": (1, "3403a497536a5c37c6a28f46c7ec3a6e09c71b20ee11bec196c93e5497041541"),
    "load_test": (1, "766519b73e92f859505c06b814e071cbb9bac898e32679574129339c1b67d1ab"),
    "logprob": (1, "20a0ab8ec361396ff16a7380f0eb7bf218d9a979c69ea3b8e7297346ec2c6f35"),
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


def _prefix():
    config = runner._check_config(_config(checks=["prefix"]), "prefix")
    return _requests(list(workloads.prefix_requests(config, nonce=lambda: NONCE)))


def _consistency():
    config = runner._check_config(_config(
        checks=["consistency"],
        consistency={"prompts_per_shape": len(workloads.COPY_PASSAGES) + 1,
                     "shapes": ["short", "long"]},
    ), "consistency")
    return [_requests([spec]) for _, _, spec in list(workloads.consistency_requests(config))]


def _tools():
    config = runner._check_config(_config(checks=["tools"], forced_tool_diagnostic=True), "tools")
    return _requests([
        workloads.tool_request(config, fixture, mode, forced=forced)
        for forced in (False, True)
        for fixture in workloads.resolved_tool_fixtures(config)
        for mode in workloads.resolved_tool_modes(config)
    ])


def _fixed_sessions():
    config = runner._check_config(_config(checks=["sessions"]), "sessions")
    return _requests(request for mode in config.sessions.modes
                     for index in range(config.sessions.sessions)
                     for request in workloads.fixed_session_requests(config, config.sessions, mode, index))


def _load_guard_cases():
    # Cover the primary source and the independently configured migrated source
    # selected by both arrival_window and _minimum_request_budget.
    settings = LoadTest(migrated_tests=[{"source": "arrivals", "settings": LoadTestSettings().model_dump()}])
    return settings, dict(load_test.settings_sets(settings)), load_test.test_points(settings)


def _load_test():
    settings, sources, points = _load_guard_cases()
    config = _config(checks=["load"])
    calibration = {(size, variant): {"filler_chars": 400}
                   for size in ("short", "medium", "long") for variant in ("short_answer", "long_output")}
    return {
        "arrivals": [{"schedule": load_test.planned_arrivals(sources[point["source"]], point)}
                     for point in points],
        "requests": [_requests([load_test.build_spec(SimpleNamespace(config=config), sources[point["source"]],
                                                    point, shape, calibration, nonce=NONCE)])
                     for point in points for shape in load_test.point_shapes(sources[point["source"]], point)],
    }


def _multi_user_cases():
    settings = MultiUser()
    config = _config()
    load_settings = LoadTestSettings(profile=settings.profile,
                                     request_timeout_seconds=config.request_timeout_seconds)
    calibration = {(size, "short_answer" if output == "short" else "long_output"): {"filler_chars": 400}
                   for size, output in (shape.split("_") for shape in SHAPES)}
    for shape in SHAPES:
        for level in settings.levels:
            point = {"id": f"multi_user_{level}_measurement", "type": "combinations", "workload": "combinations",
                     "pattern": "concurrent", "level": level, "rate": None, "bursts": False, "source": "primary"}
            yield config, load_settings, point, shape, calibration


def _multi_user_plan_settings():
    return [
        MultiUser(),
        MultiUser(
            scenarios=["repeated_requests", "arrivals", "recovery", "noisy_neighbors", "cancellation"],
            levels=[2, 5], shapes=list(SHAPES), warmup_seconds=1, duration_seconds=3,
            think_time_seconds={"values": [0, .25], "weights": [1, 1]},
            arrivals={"requests_per_second": 2, "activations": {"user-02": .5},
                      "bursts": [{"user_id": "user-01", "at_seconds": 1, "requests": 3,
                                  "spacing_seconds": .25},
                                 {"user_id": "user-02", "at_seconds": 1, "requests": 2}]},
            noisy_neighbors={"heavy_profiles": ["long_short", "short_long", "long_long"]},
        ),
        MultiUser(scenarios=["noisy_neighbors"], levels=[2],
                  noisy_neighbors={"heavy_user_ids": ["user-02"]}),
        MultiUser(conversations={}),
        MultiUser(conversations={"profile": "performance", "modes": ["live"], "workloads": [
            {"name": "chat", "turns": ["First", "Next"], "weight": 1},
            {"name": "summary", "turns": ["Read", "Summarize"], "weight": 2},
        ]}),
    ]


def _multi_user_window_plan(window):
    # Only execution inputs: omit placeholder timing, counters and accounting.
    fields = {"id", "scenario", "population", "phase", "planned_duration_seconds", "role",
              "requests_per_second", "control_window_id", "sustained_window_id", "control_work_count",
              "work_sequence_start", "trigger", "heavy_profile", "heavy_user_ids", "selected_user_id"}
    user_fields = {"user_id", "profile", "conversation_workload", "think_time_plan", "cohort", "activation_seconds"}
    offer_fields = {"index", "user_id", "profile", "cohort", "offset_seconds", "origin", "burst_index"}
    return {**{key: value for key, value in window.items() if key in fields},
            "users": [{key: value for key, value in user.items() if key in user_fields}
                      for user in window["users"]],
            **({"offers": [{key: value for key, value in offer.items() if key in offer_fields}
                           for offer in window["offers"]]} if "offers" in window else {})}


def _multi_user_plan(settings):
    # execute() consumes these exact windows, with include_offers=True by default.
    observation = multi_user._planned_observation(settings)
    return {"population": observation["population"],
            "windows": [_multi_user_window_plan(window) for window in observation["windows"]]}


def _multi_user():
    return {"plans": [_multi_user_plan(settings) for settings in _multi_user_plan_settings()],
            "requests": _requests(load_test.build_spec(SimpleNamespace(config=config), settings, point, shape,
                                                       calibration, nonce=NONCE)
                                  for config, settings, point, shape, calibration in _multi_user_cases())}


async def _session_plan():
    from tests.test_workload_builder_dispatch import sampled_guard_requests

    plans, requests = await sampled_guard_requests()
    from giraffe.session_sampling import session_plan
    from giraffe.models import Sessions

    fixed = Sessions(sampling={"think_time_seconds": {"values": [0, .25], "weights": [1, 1]}})
    return {"sampled_schedule": [plan["turns"] for plan in plans],
            "fixed_schedule": [plan["turns"] for plan in session_plan(fixed)],
            "requests": _requests(requests)}


def _scale_down():
    return _requests(resolve_fixtures(ScaleDownConfig(target="service"), builtin_fixtures(_config())))


def _logprob():
    config = runner._check_config(_config(checks=["consistency"], consistency={"logprobs": {}}), "consistency")
    templates = logprob_workload.logprob_fixtures(config, builtin_fixtures(config))
    return _requests(request for shape in config.consistency.shapes
                     for level in dict.fromkeys([1, *config.consistency.levels])
                     for phase, rounds in (("priming", config.consistency.logprobs.priming_rounds),
                                           ("measured", config.consistency.repetitions))
                     for repeat in range(rounds)
                     for request in logprob_workload.logprob_requests(config, templates, shape, level, phase, repeat))


BUILDERS = {"prefix": _prefix, "consistency": _consistency, "tools": _tools,
            "fixed_sessions": _fixed_sessions, "session_plan": _session_plan, "multi_user": _multi_user,
            "scale_down": _scale_down, "load_test": _load_test, "logprob": _logprob}


async def _digest(name):
    import inspect

    value = BUILDERS[name]()
    if inspect.isawaitable(value):
        value = await value
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _failure(name):
    return (f"The {name} workload's generated requests changed. If a server would see different requests, "
            f"bump WORKLOAD_VERSIONS['{name}'] in src/giraffe/workload_versions.py, then update PINNED here. "
            "If they did not change, the digest inputs are too broad.")


@pytest.mark.parametrize("name", PINNED)
async def test_generated_requests_match_workload_version(name, local_tokenizer):
    digest = await _digest(name)
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
    (["sessions"], {}, {"fixed_sessions"}),
    (["sessions"], {"sessions": {"sampling": {}}}, {"fixed_sessions", "session_plan"}),
    (["consistency"], {}, {"consistency"}),
    (["consistency"], {"consistency": {"logprobs": {}}}, {"logprob"}),
    (["gateway_access"], {}, {"gateway_access"}),
    (["gateway_access"], {"gateway_access": {}}, {"gateway_access"}),
    (["tools"], {}, {"tools"}),
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
    assert identity["workload_versions"] == {"tool_conversations": WORKLOAD_VERSIONS["tool_conversations"]}
    assert identity["resolved_tool_checks"]["conversations"] == [fixture.model_dump(mode="json")]


@pytest.mark.parametrize("shared_checks", [["serving"], ["load"]])
def test_shared_identity_removes_gateway_versions_and_filters_selected_builders(shared_checks, monkeypatch):
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
    monkeypatch.setitem(WORKLOAD_VERSIONS, "prefix", 2)
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


def test_gateway_settings_preserve_shared_comparability_but_selected_builder_version_does_not(monkeypatch):
    from giraffe.reporting import _settings_reasons
    from giraffe.runner import build_workload_identity
    from tests.test_reporting import make_report

    def report(profile):
        config = _config(checks=["serving", "prefix", "gateway_access"],
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

    monkeypatch.setitem(WORKLOAD_VERSIONS, "prefix", 2)
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
def test_prefix_bump_does_not_change_load_identity(checks, monkeypatch):
    from giraffe.runner import build_workload_identity

    config = _config(checks=checks)
    before, _ = build_workload_identity(config)
    monkeypatch.setitem(WORKLOAD_VERSIONS, "prefix", 2)
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
def test_multi_user_session_plan_version_follows_all_conversations(sampling, monkeypatch):
    from giraffe.runner import build_workload_identity

    config = _config(checks=["multi_user"], multi_user={"conversations": {"sampling": sampling}})
    before, _ = build_workload_identity(config)
    monkeypatch.setitem(WORKLOAD_VERSIONS, "session_plan", 2)
    after, _ = build_workload_identity(config)
    assert (fixture_hash(before) != fixture_hash(after)) is True


def test_consistency_guard_covers_second_copy_passage(monkeypatch):
    before = _consistency()
    passages = list(workloads.COPY_PASSAGES)
    passages[1] = passages[1].replace("brass key", "glass key")
    monkeypatch.setattr(workloads, "COPY_PASSAGES", tuple(passages))
    assert fixture_hash(_consistency()) != fixture_hash(before)


def test_prefix_guard_covers_same_length_label_change(monkeypatch):
    before = _prefix()
    monkeypatch.setattr(workloads, "LABEL_INSTRUCTION", workloads.LABEL_INSTRUCTION.replace("K7P4", "K7P5"))
    assert fixture_hash(_prefix()) != fixture_hash(before)


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
    expected = list(workloads.prefix_requests(runner._check_config(config, "prefix"), nonce=lambda: NONCE))
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
    expected = list(workloads.consistency_requests(runner._check_config(config, "consistency")))
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


async def test_label_instruction_changes_only_prefix_guard(monkeypatch, local_tokenizer):
    before = {name: await _digest(name) for name in BUILDERS}
    monkeypatch.setattr(workloads, "LABEL_INSTRUCTION", "\nReturn the box label K7P4 only.")
    after = {name: await _digest(name) for name in BUILDERS}
    assert {name for name in BUILDERS if before[name] != after[name]} == {"prefix"}


def test_multi_user_guard_covers_long_output_instruction(monkeypatch):
    before = fixture_hash(_multi_user())
    original = concurrent_load.prompt

    def changed(chars, variant, **kwargs):
        return original(chars, variant, **kwargs).replace("every whole number", "every whole integer") if variant == "long_output" else original(chars, variant, **kwargs)

    monkeypatch.setattr(concurrent_load, "prompt", changed)
    assert fixture_hash(_multi_user()) != before


def test_load_guard_covers_dispatch_arrival_offsets(monkeypatch):
    before = fixture_hash(_load_test())
    original = load_test.planned_arrivals

    def changed(settings, point):
        offers, overflow = original(settings, point)
        return [{**offer, "offset_seconds": offer["offset_seconds"] / 2} for offer in offers], overflow

    monkeypatch.setattr(load_test, "planned_arrivals", changed)
    assert fixture_hash(_load_test()) != before


def test_fixed_session_guard_covers_appended_assistant_history(monkeypatch):
    before = fixture_hash(_fixed_sessions())
    original = workloads.session_history

    def changed(*args):
        history = original(*args)
        history[-1]["content"] = history[-1]["content"].lower()
        return history

    monkeypatch.setattr(workloads, "session_history", changed)
    assert fixture_hash(_fixed_sessions()) != before


async def test_session_plan_guard_covers_prepared_filler(monkeypatch, local_tokenizer):
    from giraffe import local_tokenizer as sizing

    before = await _digest("session_plan")
    original = sizing.prepare_messages

    async def changed(run, target, scenario, build_messages, *args, **kwargs):
        def messages(chars):
            return [{**message, "content": message["content"].replace("neutral", "typical")}
                    for message in build_messages(chars)]
        return await original(run, target, scenario, messages, *args, **kwargs)

    monkeypatch.setattr(sizing, "prepare_messages", changed)
    assert await _digest("session_plan") != before


def test_logprob_guard_covers_final_top_logprobs(monkeypatch):
    before = fixture_hash(_logprob())
    original = logprob_workload.logprob_requests

    def changed(*args):
        requests = original(*args)
        for request in requests:
            request.options["top_logprobs"] += 1
        return requests

    monkeypatch.setattr(logprob_workload, "logprob_requests", changed)
    assert fixture_hash(_logprob()) != before


def test_multi_user_guard_covers_population_assignment(monkeypatch):
    from giraffe import multi_user

    before = fixture_hash(_multi_user())
    original = multi_user.population_plan

    def changed(settings):
        population = original(settings)
        population[0]["profile"] = "short_long"
        return population

    monkeypatch.setattr(multi_user, "population_plan", changed)
    assert fixture_hash(_multi_user()) != before


def test_multi_user_guard_covers_offer_offsets(monkeypatch):
    from giraffe import multi_user

    before = fixture_hash(_multi_user())
    original = multi_user._offer_plan

    def changed(window, settings):
        offers = original(window, settings)
        for offer in offers:
            offer["offset_seconds"] /= 2
        return offers

    monkeypatch.setattr(multi_user, "_offer_plan", changed)
    assert fixture_hash(_multi_user()) != before
