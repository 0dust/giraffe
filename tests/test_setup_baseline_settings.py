"""Setup compares the immutable baseline settings, not its mutable source run."""
from giraffe.models import RunConfig
from giraffe.reporting import write_report
from tests.test_web import browser, report


async def test_bootstrap_baseline_settings_survive_source_replacement(tmp_path):
    old = RunConfig(targets=[{'name': 'local', 'url': 'http://localhost/v1', 'model': 'm'}],
                    request_timeout_seconds=15)
    write_report(report(old), tmp_path / 'original')
    async with browser(tmp_path) as client:
        saved = await client.post('/api/baselines', json={'run_id': 'original', 'name': 'Before'})
        assert saved.status_code == 201, saved.text
        identifier = saved.json()['id']
        current = old.model_copy(update={'request_timeout_seconds': 300})
        write_report(report(current), tmp_path / 'original')
        bootstrap = (await client.get('/api/bootstrap')).json()
        assert bootstrap['baseline_configs'][identifier]['request_timeout_seconds'] == 15
        assert bootstrap['baseline_configs'][identifier]['targets'][0]['url'] == 'http://localhost/v1'
        assert not any('api_key' == key for key in bootstrap['baseline_configs'][identifier]['targets'][0])
