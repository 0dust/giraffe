"""Browser asset URLs follow the package version and the served file contents."""

import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import httpx
import pytest

from giraffe import __version__, web


@pytest.fixture
def static_copy(tmp_path, monkeypatch):
    source = Path(web.__file__).with_name('static')
    static = tmp_path / 'static'
    static.mkdir()
    for name in ('index.html', 'app.js', 'boot.js', 'styles.css', 'form-config.mjs'):
        (static / name).write_bytes((source / name).read_bytes())
    monkeypatch.setattr(web, '__file__', str(tmp_path / 'web.py'))
    return static


def asset_urls(html):
    return {Path(urlsplit(url).path).name: url for url in re.findall(
        r'(?:src|href)="(/static/[^\"]+)"', html) if urlsplit(url).path.endswith(('.js', '.css'))}


@pytest.mark.parametrize('filename', ['app.js', 'boot.js', 'styles.css'])
async def test_index_versions_each_asset_by_package_and_content(static_copy, tmp_path, filename):
    app = web.create_app(tmp_path / 'runs')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        before = asset_urls((await client.get('/')).text)
        for name, url in before.items():
            response = await client.get(url)
            assert response.status_code == 200
            version = parse_qs(urlsplit(url).query)['v'][0]
            assert version == f'{__version__}-{hashlib.sha256(response.content).hexdigest()[:16]}'
        path = static_copy / filename
        path.write_text(path.read_text() + '\n/* asset change */\n')
        after = asset_urls((await client.get('/')).text)
        assert before[filename] != after[filename]
        assert all(before[name] == after[name] for name in before if name != filename)


async def test_module_dependency_change_updates_import_and_entry_url(static_copy, tmp_path):
    app = web.create_app(tmp_path / 'runs')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        before = asset_urls((await client.get('/')).text)['app.js']
        before_script = (await client.get(before)).text
        dependency = static_copy / 'form-config.mjs'
        dependency.write_text(dependency.read_text() + '\n// dependency change\n')
        after = asset_urls((await client.get('/')).text)['app.js']
        after_script = (await client.get(after)).text
        assert after != before
        assert after_script != before_script
        imported = re.search(r"from '(\./form-config\.mjs\?[^']+)'", after_script).group(1)
        expected = f'{__version__}-{hashlib.sha256(dependency.read_bytes()).hexdigest()[:16]}'
        assert parse_qs(urlsplit(imported).query)['v'] == [expected]
        assert (await client.get('/static/' + imported[2:])).content == dependency.read_bytes()


async def test_generated_assets_keep_validators_and_invalidate_old_content(static_copy, tmp_path):
    app = web.create_app(tmp_path / 'runs')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        for url in ['/', '/static/app.js']:
            before = await client.get(url)
            assert before.headers['etag'] and before.headers['last-modified']
            unchanged = await client.get(url, headers={'If-None-Match': before.headers['etag']})
            assert unchanged.status_code == 304
            assert not unchanged.content
            script = static_copy / 'app.js'
            script.write_text(script.read_text() + '\n/* validator change */\n')
            changed = await client.get(url, headers={
                'If-None-Match': before.headers['etag'],
                'If-Modified-Since': before.headers['last-modified'],
            })
            assert changed.status_code == 200
            assert changed.headers['etag'] != before.headers['etag']
