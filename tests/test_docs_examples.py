import json
import re
import shlex
from pathlib import Path

import httpx
import pytest
import yaml

from giraffe.cli import main, parser
from giraffe.models import CHECK_NAMES, DEFAULT_CHECKS, RunConfig
from giraffe.planning import request_plan
from giraffe.open_files import open_file_guard


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / 'examples').iterdir())
README = (ROOT / 'README.md').read_text()
RUNNING = (ROOT / 'docs' / 'running.md').read_text()
DOCUMENTS = [README, RUNNING, *((ROOT / 'docs' / name).read_text()
                               for name in ('workloads.md', 'resources.md', 'offline.md'))]


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda path: path.name)
def test_examples_validate_and_preview_without_traffic(path, monkeypatch, capsys):
    def no_traffic(*args, **kwargs):
        pytest.fail('Documentation validation sent traffic')

    monkeypatch.setattr(httpx.AsyncClient, 'send', no_traffic)
    assert main(['validate', '--config', str(path)]) == 0
    validated = capsys.readouterr()
    assert 'Configuration is valid. No traffic was sent.' in validated.out
    assert 'Warning:' not in validated.out
    assert not validated.err
    assert main(['validate', '--config', str(path), '--output-format', 'json']) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation['migration_notes'] == []
    assert validation['warnings'] == []
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
    assert all(quota is None or quota >= plan['conservative_requests_per_target']
               for quota in plan['target_quotas'].values()), plan['warnings']
    notices = [notice for name in ('time_warnings', 'request_warnings', 'load_warnings',
                                  'concurrency_warnings', 'capacity_warnings') for notice in plan[name]]
    assert set(plan['warnings']) == {notice['msg'] for notice in notices}
    assert all(notice['fields'] for notice in notices)
    assert {notice['kind'] for notice in plan['request_warnings']} <= {'adaptive_risk', 'incomplete_coverage'}
    assert {notice['kind'] for notice in plan['time_warnings']} <= {'estimate_risk', 'incomplete_coverage'}



def test_readme_quickstart_uses_shared_defaults_and_model_discovery():
    install = README.split('### 1. Install\n', 1)[1].split('\n### ', 1)[0]
    assert 'git clone' not in install
    assert 'python3 -m venv .venv' in install
    assert 'source .venv/bin/activate' in install
    assert 'python -m pip install "giraffe-llm==0.1.0a1"' in install
    block = README.split('### 2. Start your run\n', 1)[1].split('\n### ', 1)[0]
    command = next(line for line in block.splitlines() if line.startswith('giraffe run '))
    assert command == 'giraffe run --url http://127.0.0.1:11434/v1 --html'
    args = parser().parse_args(shlex.split(command)[1:])
    assert args.model is None
    assert args.checks is None
    assert ', '.join(DEFAULT_CHECKS) in block
    assert 'auto-detected when only one is served' in block
    assert '--model MODEL_ID' in block
    assert '10,000 ms' in block and '--first-output-ms' in block


def test_docs_use_current_flags_and_describe_output_controls():
    for document in DOCUMENTS:
        assert '--concurrency' not in document
    assert '--output-format json' in README and '--json' in README
    assert '--structured-output' in README and '--verbose' in README


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
    assert all(quota is None or quota >= plan['known_per_target'] for quota in plan['target_quotas'].values())


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


@pytest.mark.parametrize('name', ['local', 'vllm', 'llamacpp', 'lmdeploy', 'tgi', 'tensorrt-llm', 'sglang'])
def test_engine_examples_inherit_shared_defaults(name):
    values = yaml.safe_load((ROOT / 'examples' / f'{name}.yaml').read_text())
    assert 'checks' not in values
    config = RunConfig.model_validate(values)
    assert config.checks == list(DEFAULT_CHECKS)
    assert not request_plan(config)['warnings']


def test_sglang_example_uses_default_port_and_metrics_profile():
    values = yaml.safe_load((ROOT / 'examples' / 'sglang.yaml').read_text())
    target = RunConfig.model_validate(values).targets[0]
    assert target.url == 'http://127.0.0.1:30000/v1'
    assert target.metrics_profile == 'sglang'
    assert 'examples/sglang.yaml' in README
