"""The report subprocess must use the same Giraffe installation as its parent."""

import json
import os

from fastapi import HTTPException
import pytest

from giraffe import web
from giraffe.models import RunConfig
from giraffe.reporting import write_report
from tests.test_web import config, report


def save_report(root):
    write_report(report(RunConfig.model_validate(config())), root / "saved")


def test_report_helper_ignores_decoy_package_on_cwd_and_pythonpath(tmp_path, monkeypatch):
    save_report(tmp_path)
    decoy = tmp_path / "decoy"
    package = decoy / "giraffe"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "decoy"\n')
    (package / "web.py").write_text('raise RuntimeError("decoy Giraffe imported")\n')
    monkeypatch.chdir(decoy)
    monkeypatch.setenv("PYTHONPATH", str(decoy) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    helper = web._ReportProcess()
    try:
        status, body = helper.read(tmp_path, "saved", "detail")
        assert status == 200
        assert json.loads(body)["report"]["request_count"] == 1
    finally:
        helper.close()


@pytest.mark.parametrize("identity", ["version", "package"])
def test_report_helper_rejects_identity_mismatch_and_closes(tmp_path, monkeypatch, identity):
    save_report(tmp_path)
    popen = web.subprocess.Popen
    processes = []

    def mismatched_helper(command, **kwargs):
        command = list(command)
        attribute, value = (("__version__", "different-version") if identity == "version"
                            else ("__file__", str(tmp_path / "different" / "giraffe" / "__init__.py")))
        command[-1] = command[-1].replace(
            "_report_process_main(protocol)",
            f"__import__('giraffe').{attribute} = {value!r}; _report_process_main(protocol)",
        )
        process = popen(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(web.subprocess, "Popen", mismatched_helper)
    helper = web._ReportProcess()
    try:
        with pytest.raises(HTTPException) as caught:
            helper.read(tmp_path, "saved", "detail")
        assert caught.value.status_code == 503
        assert "identity mismatch" in caught.value.detail
        assert "Restart giraffe ui" in caught.value.detail
        assert helper.process is None
        assert processes[0].poll() is not None
    finally:
        helper.close()


@pytest.mark.parametrize('mismatch', [False, True])
async def test_report_api_uses_pinned_helper_and_preserves_identity_error(tmp_path, monkeypatch, mismatch):
    from tests.test_web import browser

    save_report(tmp_path)
    monkeypatch.setattr(web, '_ISOLATED_REPORT_BYTES', 0)
    if mismatch:
        popen = web.subprocess.Popen
        def mismatched_helper(command, **kwargs):
            command = list(command)
            command[-1] = command[-1].replace('_report_process_main(protocol)',
                "__import__('giraffe').__version__ = 'stale'; _report_process_main(protocol)")
            return popen(command, **kwargs)
        monkeypatch.setattr(web.subprocess, 'Popen', mismatched_helper)
    else:
        decoy = tmp_path / 'decoy' / 'giraffe'
        decoy.mkdir(parents=True)
        (decoy / '__init__.py').write_text('__version__ = "stale"\n')
        (decoy / 'web.py').write_text('raise RuntimeError("decoy imported")\n')
        monkeypatch.chdir(decoy.parent)
        monkeypatch.setenv('PYTHONPATH', str(decoy.parent))
    async with browser(tmp_path) as client:
        response = await client.get('/api/runs/saved')
        assert response.status_code == (503 if mismatch else 200), response.text
        if mismatch:
            assert 'Report reader identity mismatch' in response.json()['detail']
        else:
            assert response.json()['report']['request_count'] == 1


@pytest.mark.parametrize('pythonpath', [None, ''])
def test_report_helper_does_not_import_dependencies_from_cwd(tmp_path, monkeypatch, pythonpath):
    save_report(tmp_path)
    hostile = tmp_path / 'world-writable'
    hostile.mkdir()
    marker = tmp_path / 'imported-httpx'
    (hostile / 'httpx.py').write_text(
        f'open({str(marker)!r}, "w").write("imported")\n'
        'raise RuntimeError("planted dependency imported")\n')
    monkeypatch.chdir(hostile)
    if pythonpath is None:
        monkeypatch.delenv('PYTHONPATH', raising=False)
    else:
        monkeypatch.setenv('PYTHONPATH', pythonpath)
    helper = web._ReportProcess()
    result = None
    try:
        try:
            result = helper.read(tmp_path, 'saved', 'detail')
        except HTTPException:
            pass
    finally:
        helper.close()
    assert not marker.exists(), 'helper executed a dependency planted in its working directory'
    assert result is not None
    status, body = result
    assert status == 200 and json.loads(body)['report']['request_count'] == 1


def test_report_helper_keeps_site_packages_after_stdlib(tmp_path, monkeypatch):
    from pathlib import Path

    save_report(tmp_path)
    source_root = str(Path(web.__file__).resolve().parent.parent)
    site_packages = tmp_path / 'site-packages'
    site_packages.mkdir()
    marker = tmp_path / 'imported-fractions'
    (site_packages / 'fractions.py').write_text(
        f'open({str(marker)!r}, "w").write("imported")\n'
        'raise RuntimeError("site-packages shadowed the standard library")\n')
    monkeypatch.setattr(web, '__file__', str(site_packages / 'giraffe' / 'web.py'))
    monkeypatch.setenv('PYTHONPATH', source_root)
    popen = web.subprocess.Popen

    def probe_stdlib(command, **kwargs):
        command = list(command)
        command[-1] = command[-1].replace('from giraffe.web import',
            "sys.modules.pop('fractions', None); import fractions; from giraffe.web import")
        return popen(command, **kwargs)

    monkeypatch.setattr(web.subprocess, 'Popen', probe_stdlib)
    helper = web._ReportProcess()
    result = None
    try:
        try:
            result = helper.read(tmp_path, 'saved', 'detail')
        except HTTPException:
            pass
    finally:
        helper.close()
    assert not marker.exists(), 'helper preferred site-packages over a standard-library module'
    assert result is not None and result[0] == 200
