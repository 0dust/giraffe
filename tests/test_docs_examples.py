import json
import re
import shlex
from pathlib import Path

import httpx
import pytest
import yaml

from giraffe.cli import configuration, main, parser
from giraffe.models import CHECK_NAMES, RunConfig
from giraffe.planning import request_plan
from giraffe.open_files import open_file_guard


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / 'examples').glob('*.yaml'))
README = (ROOT / 'README.md').read_text()
RUNNING = (ROOT / 'docs' / 'running.md').read_text()
DOCUMENTS = [README, RUNNING, *((ROOT / 'docs' / name).read_text()
                               for name in ('workloads.md', 'resources.md', 'offline.md'))]


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.name)
def test_yaml_examples_validate_and_preview_without_traffic(path, monkeypatch, capsys):
    def no_traffic(*args, **kwargs):
        pytest.fail('Documentation validation sent traffic')

    monkeypatch.setattr(httpx.AsyncClient, 'send', no_traffic)
    assert main(['validate', '--config', str(path)]) == 0
    assert 'Configuration is valid. No traffic was sent.' in capsys.readouterr().out
    assert main(['preview', '--config', str(path)]) == 0
    preview = json.loads(capsys.readouterr().out)
    config = RunConfig.model_validate(yaml.safe_load(path.read_text()))
    assert preview['config'] == config.model_dump(mode='json')
    plan = request_plan(config)
    guard = open_file_guard(config)
    expected = {**plan, "open_files": guard, "resource_warnings": guard["warnings"],
                "resource_issues": [dict(issue, blocking=True) for issue in guard["errors"]]
                + [dict(issue, blocking=False) for issue in guard["warnings"]]}
    expected["warnings"] = plan["warnings"] + [issue["msg"] for issue in guard["warnings"]]
    assert preview['request_plan'] == expected
    assert all(quota >= plan['conservative_requests_per_target']
               for quota in plan['target_quotas'].values()), plan['warnings']
    notices = [notice for name in ('time_warnings', 'request_warnings', 'load_warnings',
                                  'concurrency_warnings', 'capacity_warnings') for notice in plan[name]]
    assert set(plan['warnings']) == {notice['msg'] for notice in notices}
    assert all(notice['fields'] for notice in notices)
    assert {notice['kind'] for notice in plan['request_warnings']} <= {'adaptive_risk', 'incomplete_coverage'}
    assert {notice['kind'] for notice in plan['time_warnings']} <= {'estimate_risk', 'incomplete_coverage'}



def test_readme_quickstart_uses_sequential_core_checks():
    install = README.split('### 1. Install\n', 1)[1].split('\n### ', 1)[0]
    assert 'git clone' not in install
    assert 'python3 -m venv .venv' in install
    assert 'source .venv/bin/activate' in install
    assert 'python -m pip install "giraffe-llm==0.1.0a1"' in install
    block = README.split('### 2. Start your run\n', 1)[1].split('\n### ', 1)[0]
    command = next(line for line in block.replace('\\\n', ' ').splitlines()
                   if line.startswith('giraffe run '))
    config = configuration(parser().parse_args(shlex.split(command)[1:]))
    assert config.targets[0].url == 'http://127.0.0.1:11434/v1'
    assert config.targets[0].model == 'llama3.2:3b'
    assert config.checks == ['access', 'serving', 'correctness']
    assert config.concurrency == 1
    assert request_plan(config)['known_per_target'] == 13


def test_running_guide_exit_codes_match_cli_help(capsys):
    with pytest.raises(SystemExit) as result:
        main(['run', '--help'])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    rows = re.findall(r'^\| `([0-4])` \| ([^|]+) \|$', RUNNING, re.MULTILINE)
    assert len(rows) == 5
    for code, meaning in rows:
        assert re.search(rf'^  {code}  {re.escape(meaning.lower())}$', help_text, re.MULTILINE)


@pytest.mark.parametrize('block', [block for document in DOCUMENTS
                                  for block in re.findall(r'```yaml\n(.*?)```', document, re.DOTALL)],
                         ids=lambda block: 'targets' if block.startswith('targets:') else 'traffic')
def test_documented_yaml_settings_validate_and_cover_fixed_work(block):
    values = yaml.safe_load(block)
    values.setdefault('targets', [{'name': 'local', 'url': 'http://localhost/v1', 'model': 'example'}])
    config = RunConfig.model_validate(values)
    plan = request_plan(config)
    assert all(quota >= plan['known_per_target'] for quota in plan['target_quotas'].values())


@pytest.mark.parametrize('command', [line for document in DOCUMENTS
                                     for block in re.findall(r'```sh\n(.*?)```', document, re.DOTALL)
                                     for line in block.replace('\\\n', ' ').splitlines()
                                     if line.startswith('giraffe ')])
def test_documented_shell_commands_use_supported_cli_flags(command):
    args = shlex.split(command)[1:]
    if '>' in args:
        args = args[:args.index('>')]
    parser().parse_args(args)


def test_readme_catalog_matches_available_checks():
    section = README.split('## Tests\n', 1)[1].split('\n## ', 1)[0]
    rows = re.findall(r'^\| ([^|]+) · `([a-z_]+)` \|', section, re.MULTILINE)
    assert len(rows) == len(CHECK_NAMES)
    assert {check_id: title.strip() for title, check_id in rows} == CHECK_NAMES


@pytest.mark.parametrize('name', ['local', 'vllm'])
def test_primary_examples_exercise_both_intended_concurrent_levels(name):
    values = yaml.safe_load((ROOT / 'examples' / f'{name}.yaml').read_text())
    config = RunConfig.model_validate(values)
    plan = request_plan(config)
    assert config.load_test.patterns.concurrent.levels == [1, 2]
    assert {point['level'] for point in plan['load_test']['points']
            if point['pattern'] == 'concurrent'} == {1, 2}
    assert not plan['errors']
    assert not any(notice.get('check') == 'load' for notice in plan['concurrency_warnings'])
