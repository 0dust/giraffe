"""Socket-free CLI contracts for CI, init and discoverable help."""
import asyncio
import json
import re
import signal

import pytest
import yaml

from giraffe.cli import main, parser
from giraffe.models import CheckResult, RunConfig, RunReport


@pytest.mark.parametrize('policy,verdict,expected', [
    ('inconclusive', 'pass', 0), ('inconclusive', 'fail', 1),
    ('inconclusive', 'inconclusive', 2), ('fail', 'inconclusive', 0),
    ('fail', 'fail', 1), ('never', 'inconclusive', 0), ('never', 'fail', 0),
    ('never', 'blocked', 3),
])
def test_verdict_policy_and_quiet(policy, verdict, expected, tmp_path, monkeypatch, capsys):
    async def run(config, **kwargs):
        return RunReport(run_id='ci', started_at='now', finished_at='now', overall=verdict,
                         manifest={'config': config.model_dump(mode='json')})
    monkeypatch.setattr('giraffe.cli._run', run)
    async def tokenizers(*a):
        pass
    monkeypatch.setattr('giraffe.local_tokenizer.ensure_tokenizers', tokenizers)
    assert main(['run', '--url', 'http://server/v1', '--model', 'tiny', '--skip-preflight',
                 '--output', str(tmp_path / 'run'), '--fail-on', policy, '--quiet']) == expected
    captured = capsys.readouterr()
    assert captured.out.startswith(verdict.upper())
    assert len(captured.out.splitlines()) == 1
    assert 'Giraffe:' not in captured.err and 'Per-check' not in captured.err
    if (verdict == 'inconclusive' and policy in {'fail', 'never'}) or (verdict == 'fail' and policy == 'never'):
        assert verdict in captured.err and f'--fail-on {policy}' in captured.err


def test_compact_and_full_help(capsys):
    with pytest.raises(SystemExit) as result:
        main(['run', '--help'])
    assert result.value.code == 0
    short = capsys.readouterr().out
    assert len(re.findall(r'^  -', short, re.M)) <= 30
    assert '--max-concurrency' in short and '--no-stream' in short
    assert '--nonstream' not in short and '--structured-output' not in short
    assert '--latency-ms' not in short
    with pytest.raises(SystemExit):
        main(['run', '--help-all'])
    full = capsys.readouterr().out
    assert '--latency-ms' in full and '--unresponsive-for' in full
    with pytest.raises(SystemExit):
        main(['--help'])
    root = capsys.readouterr().out
    assert 'runs' in root and '==SUPPRESS==' not in root
    assert not re.search(r'^    (storage|cleanup|keep) ', root, re.M)


def test_concurrency_and_stream_aliases(capsys):
    from giraffe.cli import configuration
    args = ['run', '--url', 'http://server/v1', '--model', 'tiny']
    current = configuration(parser().parse_args(args + ['--max-concurrency', '8', '--no-stream']))
    assert current.concurrency == 8 and not current.stream
    assert not capsys.readouterr().err
    legacy = configuration(parser().parse_args(args + ['--nonstream', '--metrics', '--tool-calling', '--structured-output']))
    assert not legacy.stream and {'gpu', 'tools', 'json'} <= set(legacy.checks)
    notes = capsys.readouterr().err
    for flag in ('--nonstream', '--metrics', '--tool-calling', '--structured-output'):
        assert flag in notes


@pytest.mark.parametrize('engine', ['ollama', 'vllm', 'sglang', 'llamacpp', 'tgi', 'lmdeploy', 'tensorrt-llm'])
def test_init_valid_without_warnings_and_no_overwrite(engine, tmp_path, capsys):
    output = tmp_path / 'giraffe.yaml'
    assert main(['init', '--engine', engine, '--output', str(output)]) == 0
    original = output.read_text()
    assert original.count('#') >= 5
    config = yaml.safe_load(original)
    assert config['config_version'] == 2 and config['targets'][0]['metrics_profile'] != 'unknown'
    assert main(['validate', '--config', str(output), '--output-format', 'json']) == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result['warnings'] == [] and result['migration_notes'] == []
    assert main(['init', '--output', str(output)]) == 4
    assert output.read_text() == original
    assert main(['init', '--output', str(output), '--force']) == 0


@pytest.mark.parametrize('models,expected', [(['only'], 'only'), (['one', 'two'], 'replace-with-served-model-id'), ([], 'replace-with-served-model-id')])
def test_init_discovers_only_single_model(models, expected, tmp_path, monkeypatch):
    async def discover(body, **kwargs):
        assert body.url == 'http://server/v1'
        return {'models': models}
    monkeypatch.setattr('giraffe.preflight.discover_models', discover)
    output = tmp_path / 'giraffe.yaml'
    assert main(['init', '--url', 'http://server/v1', '--output', str(output)]) == 0
    assert yaml.safe_load(output.read_text())['targets'][0]['model'] == expected


def test_runs_list_and_hidden_legacy_alias(tmp_path, capsys):
    assert main(['runs', 'list', '--runs-dir', str(tmp_path)]) == 0
    current = capsys.readouterr()
    assert main(['storage', '--runs-dir', str(tmp_path)]) == 0
    old = capsys.readouterr()
    assert current.out == old.out
    assert not current.err and 'giraffe runs list' in old.err


def test_report_ci_outputs_without_html(tmp_path):
    report = RunReport(run_id='saved', started_at='now', finished_at='now', overall='pass', manifest={})
    (tmp_path / 'report.json').write_text(report.model_dump_json())
    junit, summary = tmp_path / 'junit.xml', tmp_path / 'summary.md'
    assert main(['report', str(tmp_path), '--junit', str(junit), '--summary-md', str(summary)]) == 0
    assert junit.exists() and summary.exists() and not (tmp_path / 'report.html').exists()


@pytest.mark.parametrize('failure,expected', [('model', 2), ('preflight', 3), ('config', 4), ('internal', 4)])
def test_never_does_not_remap_errors(failure, expected, tmp_path, monkeypatch, capsys):
    from giraffe.cli import ModelSelectionError
    from giraffe.preflight import PreflightError

    async def prepare(*a):
        pass
    monkeypatch.setattr('giraffe.local_tokenizer.ensure_tokenizers', prepare)
    arguments = ['run', '--url', 'http://server/v1', '--fail-on', 'never', '--quiet',
                 '--output', str(tmp_path / 'run')]
    if failure == 'model':
        def discover(*a):
            raise ModelSelectionError('Choose a model')
        monkeypatch.setattr('giraffe.cli._discover_model', discover)
    else:
        arguments += ['--model', 'tiny']
    if failure == 'preflight':
        async def blocked(*a):
            raise PreflightError('endpoint unavailable', blocking=True)
        monkeypatch.setattr('giraffe.preflight.check_target', blocked)
    else:
        arguments.append('--skip-preflight')
    if failure == 'config':
        arguments += ['--samples', '0']
    if failure == 'internal':
        async def broken(*a, **kwargs):
            raise ValueError('internal run failure')
        monkeypatch.setattr('giraffe.cli._run', broken)
    assert main(arguments) == expected
    assert capsys.readouterr().err


@pytest.mark.parametrize('policy', ['inconclusive', 'fail', 'never'])
def test_failed_verdict_preserves_recorded_client_error(
        policy, tmp_path, monkeypatch, capsys, local_tokenizer):
    from tests.test_runner import FakeClient

    class BrokenClient(FakeClient):
        bad_target = 'wrong'

        async def __aenter__(self):
            if self.target.name == 'broken':
                raise RuntimeError('client initialization defect')
            return self

    monkeypatch.setattr('giraffe.runner.LLMClient', BrokenClient)
    config = RunConfig(config_version=2, checks=['correctness'], samples=2,
                       targets=[{'name': name, 'url': 'http://server/v1', 'model': 'tiny'}
                                for name in ['wrong', 'broken']])
    source = tmp_path / 'config.json'
    source.write_text(config.model_dump_json())
    directory = tmp_path / 'run'
    exit_code = main(['run', '--config', str(source), '--skip-preflight', '--quiet', '--json',
                     '--output', str(directory), '--fail-on', policy])
    captured = capsys.readouterr()
    assert exit_code == 3
    saved = json.loads((directory / 'report.json').read_text())
    assert saved['overall'] == 'fail'
    required = [check for check in saved['checks'] if check['required']]
    assert any(check['status'] == 'fail' for check in required)
    assert any(check['status'] == 'blocked' for check in required)
    assert any('Internal error' in target.get('client_error', '')
               for target in saved['observations']['targets'].values())
    result = json.loads(captured.out)
    assert result['outcome'] == 'fail' and result['exit_code'] == 3
    assert 'verdict accepted' not in captured.err


@pytest.mark.parametrize('verdict,required,client_error,expected', [
    ('fail', True, False, 3), ('inconclusive', True, False, 3),
    ('fail', False, False, 0), ('inconclusive', False, False, 0), ('pass', False, False, 0),
    ('fail', False, True, 3), ('inconclusive', False, True, 3),
])
def test_never_distinguishes_required_and_optional_blockers(
        verdict, required, client_error, expected, tmp_path, monkeypatch, capsys):
    async def run(config, **kwargs):
        return RunReport(run_id='blocked-check', started_at='now', finished_at='now',
                         overall=verdict, manifest={'config': config.model_dump(mode='json')},
                         checks=[CheckResult(id='tools', target='local', title='Tools',
                                             status='blocked', required=required,
                                             summary='Tool support unavailable')],
                         observations={'targets': {'local': {
                             'client_error': 'Internal error in Giraffe' if client_error else None}}})

    async def prepare(*args):
        pass

    monkeypatch.setattr('giraffe.cli._run', run)
    monkeypatch.setattr('giraffe.local_tokenizer.ensure_tokenizers', prepare)
    assert main(['run', '--url', 'http://server/v1', '--model', 'tiny', '--skip-preflight',
                 '--output', str(tmp_path / 'run'), '--fail-on', 'never', '--quiet']) == expected
    captured = capsys.readouterr()
    assert captured.out.startswith(verdict.upper())
    assert ('verdict accepted' in captured.err) == (expected == 0 and verdict != 'pass')


@pytest.mark.parametrize('with_partial_report', [False, True])
def test_never_preserves_cancellation_exit(with_partial_report, tmp_path, monkeypatch, capsys):
    async def run(config, **kwargs):
        error = asyncio.CancelledError()
        if with_partial_report:
            error.giraffe_report = RunReport(
                run_id='cancelled', started_at='now', finished_at='now', overall='fail',
                manifest={'config': config.model_dump(mode='json')},
                observations={'targets': {'local': {'client_error': 'Client setup failed'}}})
        raise error

    async def prepare(*args):
        pass

    monkeypatch.setattr('giraffe.cli._run', run)
    monkeypatch.setattr('giraffe.local_tokenizer.ensure_tokenizers', prepare)
    assert main(['run', '--url', 'http://server/v1', '--model', 'tiny', '--skip-preflight',
                 '--output', str(tmp_path / 'run'), '--fail-on', 'never', '--quiet', '--json']) == 130
    captured = capsys.readouterr()
    assert json.loads(captured.out)['exit_code'] == 130
    assert 'verdict accepted' not in captured.err


@pytest.mark.parametrize('policy', ['inconclusive', 'fail', 'never'])
@pytest.mark.parametrize('stop_mode', ['SIGINT', 'SIGTERM', 'request_limit'])
def test_signal_stop_preserves_partial_evidence_and_cancellation_exit(
        policy, stop_mode, tmp_path, monkeypatch, capsys, local_tokenizer):
    from tests.test_runner import FakeClient

    handlers, removed, cleanup_calls = {}, [], []
    loop = asyncio.new_event_loop()
    loop_type = type(loop)
    loop.close()
    install, remove = loop_type.add_signal_handler, loop_type.remove_signal_handler

    def install_handler(loop, sig, callback, *args):
        install(loop, sig, callback, *args)
        handlers[sig] = (callback, args)

    def remove_handler(loop, sig):
        removed.append(sig)
        return remove(loop, sig)

    class StoppedClient(FakeClient):
        async def execute(self, spec, **kwargs):
            record = await super().execute(spec, **kwargs)
            if stop_mode != 'request_limit':
                callback, args = handlers[getattr(signal, stop_mode)]
                callback(*args)
            return record

    def cleanup(*args, **kwargs):
        cleanup_calls.append(True)
        return {'deleted': []}

    monkeypatch.setattr(loop_type, 'add_signal_handler', install_handler)
    monkeypatch.setattr(loop_type, 'remove_signal_handler', remove_handler)
    monkeypatch.setattr('giraffe.runner.LLMClient', StoppedClient)
    monkeypatch.setattr('giraffe.storage.automatic_cleanup', cleanup)
    config = RunConfig(config_version=2, checks=['correctness'], samples=3, concurrency=1,
                       max_requests=1 if stop_mode == 'request_limit' else 10,
                       targets=[{'name': 'local', 'url': 'http://server/v1', 'model': 'tiny'}])
    source, directory = tmp_path / 'config.json', tmp_path / 'run'
    source.write_text(config.model_dump_json())
    code = main(['run', '--config', str(source), '--skip-preflight', '--quiet', '--json',
                 '--output', str(directory), '--fail-on', policy, '--min-free-disk', '0',
                 '--runs-dir', str(tmp_path), '--auto-cleanup-keep-latest', '0'])
    captured = capsys.readouterr()
    saved = json.loads((directory / 'report.json').read_text())
    assert saved['overall'] == 'inconclusive'
    assert len(saved['requests']) == 1 and saved['requests'][0]['status'] == 'completed'
    assert any(check['required'] and check['status'] == 'inconclusive' for check in saved['checks'])
    assert saved['manifest']['invocation']['source'] == 'cli'
    assert set(handlers) == set(removed) == {signal.SIGINT, signal.SIGTERM}
    interrupted = stop_mode != 'request_limit'
    expected = 130 if interrupted else 2 if policy == 'inconclusive' else 0
    assert code == json.loads(captured.out)['exit_code'] == expected
    assert (saved['abort_reason'] == 'cancelled by user') == interrupted
    assert cleanup_calls == ([] if interrupted else [True])
    assert ('verdict accepted' in captured.err) == (expected == 0)


async def test_quiet_suppresses_actual_run_progress(monkeypatch, capsys):
    from giraffe.cli import _run
    from giraffe.models import RunConfig
    async def suite(config, *, progress, stop_event):
        progress({'target': 'local', 'scenario': 'warm', 'completed': 1, 'elapsed_seconds': 1})
        return RunReport(run_id='progress', started_at='now', finished_at='now',
                         overall='pass', manifest={})
    monkeypatch.setattr('giraffe.runner.run_suite', suite)
    config = RunConfig(targets=[{'name': 'local', 'url': 'http://server/v1', 'model': 'tiny'}])
    await _run(config, quiet=True, json_progress=True)
    assert capsys.readouterr().err == ''
    await _run(config)
    assert 'Warm-up' in capsys.readouterr().err


@pytest.mark.parametrize('policy,expected', [('fail', 1), ('never', 0)])
def test_baseline_regression_fails_and_exports(policy, expected, tmp_path, monkeypatch, capsys):
    import xml.etree.ElementTree as ET
    from tests.test_reporting import make_report
    old, current = make_report('before'), make_report(latency=1000)
    baseline, config = tmp_path / 'baseline.json', tmp_path / 'config.json'
    baseline.write_text(old.model_dump_json())
    config.write_text(json.dumps(current.manifest['config']))
    async def run(*a, **kwargs):
        return current
    async def prepare(*a):
        pass
    monkeypatch.setattr('giraffe.cli._run', run)
    monkeypatch.setattr('giraffe.local_tokenizer.ensure_tokenizers', prepare)
    junit, summary = tmp_path / 'ci.xml', tmp_path / 'ci.md'
    assert main(['run', '--config', str(config), '--baseline', str(baseline), '--quiet',
                 '--skip-preflight', '--fail-on', policy, '--output', str(tmp_path / 'run'),
                 '--junit', str(junit), '--summary-md', str(summary)]) == expected
    assert capsys.readouterr().out.startswith('FAIL')
    assert ET.parse(junit).findall('.//failure')
    assert json.loads((tmp_path / 'run' / 'report.json').read_text())['overall'] == 'fail'
    assert 'Baseline delta' in summary.read_text() and 'FAIL' in summary.read_text()


@pytest.mark.parametrize('host,port', [('0.0.0.0', 8765), ('::', 8768)])
def test_ui_host_token_link_and_restart_opt_in(host, port, tmp_path, monkeypatch, capsys):
    import uvicorn
    captured = {}
    class Listener:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def setsockopt(self, *args):
            pass
        def bind(self, address):
            captured['address'] = address
        def listen(self, *args):
            pass
    monkeypatch.setattr('giraffe.cli.socket.socket', lambda *a: Listener())
    def run(server, **kwargs):
        captured['app'] = server.config.app
        captured['host'] = server.config.host
    monkeypatch.setattr(uvicorn.Server, 'run', run)
    assert main(['ui', '--host', host, '--port', str(port), '--no-browser', '--allow-restart-script',
                 '--runs-dir', str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert captured['address'] == (host, port) and captured['host'] == host
    assert 'Warning:' in output.err
    token = captured['app'].state.giraffe_token
    assert f'http://localhost:{port}/?token={token}' in output.out
    assert captured['app'].state.giraffe_cookie_name == f'giraffe_token_{port}'


def test_quiet_wins_over_verbose_on_export_error(tmp_path, monkeypatch, capsys):
    async def run(config, **kwargs):
        return RunReport(run_id='disk-full', started_at='now', finished_at='now', overall='pass',
                         manifest={'config': config.model_dump(mode='json')})
    async def prepare(*a):
        pass
    def disk_full(*a, **kwargs):
        raise OSError(28, 'disk full')
    monkeypatch.setattr('giraffe.cli._run', run)
    monkeypatch.setattr('giraffe.local_tokenizer.ensure_tokenizers', prepare)
    monkeypatch.setattr('giraffe.reporting.write_report', disk_full)
    assert main(['run', '--url', 'http://server/v1', '--model', 'tiny', '--skip-preflight',
                 '--output', str(tmp_path / 'run'), '--quiet', '--verbose', '--fail-on', 'never']) == 4
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'disk full' in captured.err


@pytest.mark.parametrize('blocker', ['preflight', 'tokenizer', 'open_files'])
def test_blocked_runs_write_ci_outputs_without_changing_exit(blocker, tmp_path, monkeypatch, capsys):
    import xml.etree.ElementTree as ET
    from giraffe.local_tokenizer import TokenizerUnavailable
    from giraffe.preflight import PreflightError
    reason = f'{blocker} blocking reason'
    async def prepare(*a):
        if blocker == 'tokenizer':
            raise TokenizerUnavailable(reason)
    async def preflight(*a):
        raise PreflightError(reason, blocking=True)
    monkeypatch.setattr('giraffe.local_tokenizer.ensure_tokenizers', prepare)
    monkeypatch.setattr('giraffe.preflight.check_target', preflight)
    if blocker == 'open_files':
        monkeypatch.setattr('giraffe.open_files.open_file_guard', lambda *a, **kw: {
            'errors': [{'msg': reason}], 'warnings': []})
    monkeypatch.setattr('giraffe.cli._run', lambda *a, **kw: pytest.fail('blocked suite executed'))
    junit, summary = tmp_path / 'blocked.xml', tmp_path / 'blocked.md'
    assert main(['run', '--url', 'http://server/v1', '--model', 'tiny', '--checks', 'access,serving',
                 '--output', str(tmp_path / 'run'), '--fail-on', 'never', '--quiet',
                 '--junit', str(junit), '--summary-md', str(summary)]) == 3
    cases = ET.parse(junit).findall('.//testcase')
    assert [(c.get('name'), c.get('classname')) for c in cases] == [('access', 'local'), ('serving', 'local')]
    assert all(reason in ''.join(c.itertext()) and (c.find('error') is not None or c.find('skipped') is not None) for c in cases)
    assert '**BLOCKED**' in summary.read_text() and reason in summary.read_text()
    assert 'No report (run blocked before start)' in summary.read_text()
    assert 'Report:' not in summary.read_text()
    assert not (tmp_path / 'run' / 'report.json').exists()
    assert reason in capsys.readouterr().err
