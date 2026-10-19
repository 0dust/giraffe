import json
import re
import shlex
from pathlib import Path

import httpx
import pytest
import yaml

from giraffe.cli import configuration, main, parser
from giraffe.models import RunConfig, Target
from giraffe.planning import request_plan


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / 'examples').glob('*.yaml'))
README = (ROOT / 'README.md').read_text()


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
    assert preview['request_plan'] == plan
    assert all(quota >= plan['conservative_requests_per_target']
               for quota in plan['target_quotas'].values()), plan['warnings']
    for warning in plan['warnings']:
        assert 'configured limits prevent this case' in warning
        assert 'Large-token cases report insufficient limits' in path.read_text()



def test_readme_quickstart_uses_sequential_core_checks():
    block = README.split('## Try it in 30 seconds\n', 1)[1].split('\n## ', 1)[0]
    assert 'git clone https://github.com/0dust/giraffe.git && cd giraffe' in block
    assert 'python -m venv .venv' in block
    assert '. .venv/bin/activate' in block
    assert 'pip install -e .' in block
    command = next(line for line in block.splitlines() if line.startswith('giraffe run '))
    config = configuration(parser().parse_args(shlex.split(command)[1:]))
    assert config.targets[0].url == 'http://127.0.0.1:11434/v1'
    assert config.targets[0].model == 'llama3.2:3b'
    assert config.checks == ['access', 'serving', 'correctness']
    assert config.concurrency == 1
    assert request_plan(config)['known_per_target'] == 13
    assert 'OLLAMA_NUM_PARALLEL' in block


def test_readme_exit_codes_match_cli_help(capsys):
    with pytest.raises(SystemExit) as result:
        main(['run', '--help'])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    rows = re.findall(r'^\| `([0-4])` \| ([^|]+) \|$', README, re.MULTILINE)
    assert len(rows) == 5
    for code, meaning in rows:
        assert re.search(rf'^  {code}  {re.escape(meaning.lower())}$', help_text, re.MULTILINE)


@pytest.mark.parametrize('block', re.findall(r'```yaml\n(.*?)```', README, re.DOTALL),
                         ids=lambda block: 'targets' if block.startswith('targets:') else 'traffic')
def test_readme_yaml_settings_validate_and_cover_fixed_work(block):
    values = yaml.safe_load(block)
    values.setdefault('targets', [{'name': 'local', 'url': 'http://localhost/v1', 'model': 'example'}])
    config = RunConfig.model_validate(values)
    plan = request_plan(config)
    assert all(quota >= plan['known_per_target'] for quota in plan['target_quotas'].values())


@pytest.mark.parametrize('command', [line for block in re.findall(r'```sh\n(.*?)```', README, re.DOTALL)
                                     for line in block.replace('\\\n', ' ').splitlines()
                                     if line.startswith('giraffe ')])
def test_readme_shell_commands_use_supported_cli_flags(command):
    parser().parse_args(shlex.split(command)[1:])


@pytest.mark.parametrize('section', ['Ollama', 'vLLM'])
def test_readme_target_settings_include_required_fields(section):
    block = README.split(f'## {section}\n', 1)[1].split('\n## ', 1)[0]
    for name, field in Target.model_fields.items():
        if field.is_required():
            assert f'| `targets[0].{name}` |' in block
