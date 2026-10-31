"""Settings explanations remain available in their rendered information popovers."""

import json

from tests.test_run_limits_phase6_ui import Markup, rendered_form
from tests.test_ux_catalogue import browser_logic


def rendered(expression):
    return browser_logic('console.log(JSON.stringify(' + expression + '));')


def help_text(tree, name):
    node, = [node for node in tree.nodes if node.get('id') == f'field-{name}-help']
    assert node['popover'] == 'auto'
    return node['text']


def assert_moved(tree, name, text):
    assert help_text(tree, name).count(text) == 1
    assert not any(text in node['text'] for node in tree.nodes if 'hint' in node.get('class', '').split())


def test_run_limit_information_is_beside_each_checkbox_without_duplicate_icons():
    html = browser_logic(r'''
const markup=renderSharedRunSettings(config);
console.log(JSON.stringify({markup,cap:bootstrap.cap_explanations.concurrency.help}));
''')
    tree = Markup(html['markup'])
    for name in ('max_duration_seconds', 'max_requests', 'concurrency'):
        toggle, = tree.named(name + '_enabled')
        label = next(parent for parent in toggle['parents'] if parent['tag'] == 'label')
        line = label['parents'][-1]
        assert line['class'] == 'test-selection-line'
        info, = [node for node in tree.nodes if node.get('id') == f'field-{name}-info']
        assert info['parents'][-1] is line
        assert label not in info['parents']
        assert tree.named(name)[0]['parents'][-1] is not line
    assert html['cap'] in help_text(tree, 'concurrency')
    assert not any(node['tag'] == 'p' and node.get('class') == 'hint' for node in tree.nodes)
    assert_moved(tree, 'max_duration_seconds', 'When this time limit is reached, Giraffe stops sending new requests and cancels all requests still in progress. Tests can finish earlier. Completed results are kept; AI evaluation and report saving happen afterward. Server-side work may continue if the server does not honor cancellation.')
    assert_moved(tree, 'max_requests', 'Shared by all selected checks and split into per-endpoint quotas. Scheduled requests may use budget without reaching the server. AI evaluator calls have separate limits.')


def test_tools_and_consistency_text_is_preserved_in_popovers():
    trees = {name: Markup(rendered("renderProfileEditor({id:" + json.dumps(name) + "},config)"))
             for name in ('tools', 'consistency')}
    tools = trees['tools']
    assert_moved(tools, 'profile_tools_modes', 'Run default tests both modes when streaming is enabled, otherwise complete responses. Explicit streaming requires streaming on Connect.')
    assert_moved(tools, 'profile_tools_forced_tool_diagnostic', 'Applies to fixtures without an explicit choice rule. Giraffe never executes tools.')
    limits = browser_logic("console.log(JSON.stringify({fixtures:bootstrap.limits['tool_checks.fixtures'].maxItems,steps:bootstrap.limits['tool_checks.conversations[].steps'].maxItems,bytes:diskSize(bootstrap.limits['tool_checks.bytes'].maximum),top:bootstrap.limits['consistency.logprobs.top_logprobs'],priming:bootstrap.limits['consistency.logprobs.priming_rounds']}));")
    assert_moved(tools, 'profile_tools_fixtures', f"Use [] for the built-in weather check, or up to {limits['fixtures']} fixture objects with fixture_id, messages, tools, expected_calls (task assertions) or choice (mode: none, auto, required, named; named also needs name), and optional expected_content. choice.support is required or discover; max_calls defaults to 16; parallel_tool_calls is optional. Exact content checks the combined assistant text. The server validates fixture schemas and the {limits['bytes']} configuration limit.")
    assert_moved(tools, 'profile_tools_conversations', f"Optional bounded conversations: fixture_id, messages, tools, steps, final_content. Each step declares expected_calls with name, arguments, result and optional outcome (success or error). Put {{{{observation_id}}}} in a result and a final_content check. Up to {limits['steps']} steps; all final checks must pass. Tools stay available to detect repeated operations. With conversations configured, an empty fixture list skips the built-in check.")
    assert next(node for node in tools.nodes if node.get('id') == 'field-profile_tools_fixtures-hint')['text'] == 'Use [] for the built-in weather check.'
    consistency = trees['consistency']
    assert_moved(consistency, 'profile_consistency_logprobs', f"null disables it. {{}} enables streamed probability comparisons. response_mode: complete retains complete-response diagnostics (concurrent levels cannot qualify). Options: top_logprobs ({limits['top']['minimum']}–{limits['top']['maximum']}), strict, tolerance (logprob units; required for strict), support (discover/required), priming_rounds ({limits['priming']['minimum']}–{limits['priming']['maximum']}), compare_levels. Repetitions become corpus rounds using the existing load levels; see the request estimate. This measures token probabilities, not tokens per second.")
    assert next(node for node in consistency.nodes if node.get('id') == 'field-profile_consistency_logprobs-hint')['text'] == 'null turns this off.'
    assert_moved(consistency, 'profile_consistency_levels', 'Each level is the check’s own parallelism. An accepted value is not demonstrated delivery capacity.')
    assert next(node for node in consistency.nodes if node.get('id') == 'field-profile_consistency_levels-hint')['text'] == 'For example: 1, 2, 4.'
    assert_moved(consistency, 'profile_consistency_strict', 'Require agreement fails if repeated answers differ. Wrong known answers can still fail either mode.')
    assert_moved(consistency, 'profile_consistency_equality', 'Exact text compares characters; ignore extra whitespace normalizes spacing.')


def test_session_explanations_stay_with_their_fields_and_add_task():
    tree = Markup(rendered("renderProfileEditor({id:'sessions'},config)"))
    for name, text in [
        ('sessions_profile', 'Mixed performance runs use actual replies as conversation history. Concurrent users share Simultaneous limit with other target requests. Answer quality is not scored.'),
        ('sessions_count', 'Weights divide the total conversation budget among selected tasks. Each conversation keeps its task and history. Output caps limit length; actual input/output tokens are reported.'),
        ('profile_sessions_modes', 'Prepared uses saved replies; earlier model replies uses answers from this run.'),
        ('profile_sessions_modes', 'Uses the saved fixed/live exact-repeat conversations. Their prompts, modes and concurrency can be changed below.'),
        ('profile_sessions_turns', 'Editing replaces this list. Keep prepared answers aligned with the saved conversation questions.'),
        ('profile_sessions_concurrency', 'An accepted value is not demonstrated delivery capacity.'),
        ('profile_sessions_max_tokens', 'Own answer cap for every turn. The suite default is not a ceiling.'),
    ]:
        assert_moved(tree, name, text)
    caps = rendered("[bootstrap.cap_explanations['sessions.workloads'].help,bootstrap.cap_explanations['sessions.workloads[].turns'].help]")
    for text in caps:
        assert_moved(tree, 'session_workload_caps', text)
    info, = [node for node in tree.nodes if node.get('id') == 'field-session_workload_caps-info']
    button, = [node for node in tree.nodes if node.get('data-action') == 'add-session-workload']
    assert info['parents'][-1] is button['parents'][-1]


def test_load_explanations_and_live_status_ids_are_inside_popovers():
    tree = Markup(rendered('renderLoadEditor(config)'))
    for name, text in [
        ('load_combinations_grid', 'Each selected combination runs under every selected traffic pattern and level.'),
        ('load_mixed_grid', 'Bursts use this same mix. Shares describe request counts, not tokens or time.'),
        ('load_stress_cases', 'Each selection is a separate test using the traffic patterns below. Stress prompts ask for extended output; set minimum length coverage below.'),
        ('load_data_profile', 'Used by every selected workload below. Answers may finish before their cap; actual token lengths are reported.'),
        ('load_workloads', 'Select a workload to open its settings. Selected workloads run separately; mixed traffic combines request sizes within its own test.'),
        ('load_traffic_patterns', 'Each selected pattern runs separately for combinations, mixed traffic and token stress.'),
        ('load_burst_schedule', 'Runs alone when neither Constant nor Poisson is selected. Otherwise, adds bursts to each selected arrival pattern.'),
        ('load_burst_schedule', 'New bursts do not wait for earlier requests to finish. The concurrency ceiling still applies.'),
        ('load_schedule_duration_seconds', 'Send new requests for this duration per workload, pattern and level. Requests already sent may finish within their request timeout. A run limit can stop traffic sooner. Minimum valid responses is an evidence requirement, not a stopping count.'),
        ('load_request_timing', 'For arrivals and bursts, a full client slot limit means the scheduled request is not sent. Show this as incomplete delivery; do not queue it silently or count it as an endpoint failure. Actual dispatch timing and burst spread are reported. Server queue time needs server telemetry.'),
        ('load_pass_criteria', 'Applied per response where measurable. Leave timing or speed limits blank to measure without that threshold.'),
        ('load_pass_criteria', 'A result group is a request type at one traffic pattern and level. Mixed traffic reports each selected combination separately too. Too few valid responses means inconclusive.'),
        ('load_pass_criteria', 'For scheduled arrivals, timing starts at planned arrival, including dispatch delay. Maintained-concurrency timing starts at dispatch. Correctness requires scorable answers; missing judgments stay unknown.'),
        ('load_pass_criteria', 'Incomplete traffic delivery, missing timing evidence or missing required length coverage cannot establish a capacity pass.'),
        ('load_combinations_grid', 'Each selected combination runs under every selected traffic pattern and level.'),
        ('load_mixed_grid', 'Bursts use this same mix. Shares describe request counts, not tokens or time.'),
        ('load_stress_cases', 'Each selection is a separate test using the traffic patterns below. Stress prompts ask for extended output; set minimum length coverage below.'),
    ]:
        assert_moved(tree, name, text)
    trigger_help = next(node for node in tree.nodes if node.get('id') == 'field-load_workloads_interference_trigger-help')
    assert not any(node.get('id') == 'field-load_workloads_interference_max_slowdown-info' for node in tree.nodes)
    steps, = [node for node in tree.nodes if node.get('class') == 'load-interference-steps']
    assert trigger_help in steps['parents']
    for element_id in ('load-interference-schedule', 'load-slowdown-help'):
        node, = [node for node in tree.nodes if node.get('id') == element_id]
        assert trigger_help in node['parents']
    for text in ('First, measure short answers with no long request running alongside them.', 'Repeat the same short workload. While the short answers are still streaming, send one long-input request.', 'Compare how long the short answers take to finish. Report any extra pauses in their output.', 'Short requests use Short input → Short output. The added request uses Long input → Short output from the data profile.', 'The long request is sent only while the short answers are still active. If they finish before that point, the test is inconclusive.', 'A short answer taking longer than this relative limit fails this check. Shared response-time, error and correctness criteria still apply.'):
        assert trigger_help['text'].count(text) == 1
    for text in ('Inconclusive if short answers finish before the long request is sent.', 'Leave timing or speed limits blank to measure only.'):
        assert any(text in node['text'] for node in tree.nodes if 'hint' in node.get('class', '').split())
    for element_id in ('load-output-threshold', 'load-share-note'):
        node, = [node for node in tree.nodes if node.get('id') == element_id]
        assert not any(parent.get('popover') == 'auto' for parent in node['parents'])


def test_retention_json_and_plan_help_preserve_copy_without_inline_info_buttons():
    tree = Markup(rendered_form())
    assert_moved(tree, 'retention', 'Questions and expected answers are always saved with the run. This setting controls whether response text and evaluator reasons are kept. Timings, status and scores are always saved. It does not cap disk use; storage limits do.')
    notice = rendered('inputRetentionNotice')
    assert_moved(tree, 'config_editor', notice)
    panel = next(node for node in tree.nodes if node.get('id') == 'config-panel-json')
    assert any(node.get('class') == 'hint' and node['text'] == 'Apply changes before returning to the form.' and panel in node['parents'] for node in tree.nodes)
    blueprint = Markup(rendered('renderRunBlueprint(config)'))
    assert_moved(blueprint, 'run_plan_limits', 'Optional limits you set. Tests also stop at their own durations, request counts and per-request timeouts.')
    for current in (tree, blueprint, Markup(rendered('renderLoadEditor(config)'))):
        for info in [node for node in current.nodes if node.get('class') == 'field-info']:
            assert not any(parent['tag'] in ('label', 'summary') for parent in info['parents'])
