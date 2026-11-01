"""Optional tokenizer dependencies and distributable CI configuration."""
import builtins
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest
import yaml

from giraffe import local_tokenizer as sizing
from giraffe.models import RunConfig


ROOT = Path(__file__).resolve().parents[1]


def test_hf_dependencies_are_an_extra():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    core = project["dependencies"]
    assert not any(name in item for name in ("torch", "transformers", "jinja2") for item in core)
    assert any(item.startswith("fastapi") for item in core)
    assert any(item.startswith("uvicorn") for item in core)
    extra = project["optional-dependencies"]["hf-tokenizer"]
    assert any(item.startswith("transformers") for item in extra)
    assert any(item.startswith("jinja2") for item in extra)


def block_hf_imports(monkeypatch):
    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.split(".")[0] in {"transformers", "huggingface_hub"}:
            raise ModuleNotFoundError(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)


async def test_hub_resolution_without_extra_is_optional(monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    block_hf_imports(monkeypatch)
    assert await sizing.resolve_hub_name("organization/model") is None


def test_explicit_hf_worker_reports_install_hint(monkeypatch, capsys):
    block_hf_imports(monkeypatch)
    sizing._worker("organization/model")
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == "missing_dependency"
    assert result["error"] == "Install giraffe-llm[hf-tokenizer] to use the model's own tokenizer"


async def test_automatic_hf_without_extra_uses_builtin(monkeypatch, local_tokenizer, capsys):
    worker = sizing._start_worker
    calls = []

    async def missing_hf(name, **kwargs):
        calls.append(name)
        if name != sizing.BUILTIN:
            raise sizing.TokenizerUnavailable(sizing.HF_INSTALL_HINT, code="missing_dependency")
        return await worker(name, **kwargs)

    monkeypatch.setattr(sizing, "_start_worker", missing_hf)
    monkeypatch.setattr(sizing, "_remembered_alias", lambda target: None)
    config = RunConfig(targets=[{"name": "local", "url": "http://local.invalid/v1",
                                "model": "model", "tokenizer": "auto"}], checks=["load"])
    await sizing.ensure_tokenizers(config)
    assert calls == ["model", "builtin"]
    assert config.targets[0]._tokenizer_setup["source"] == "builtin_fallback"
    assert capsys.readouterr().err == ""
    config.targets[0].tokenizer = "organization/model"
    with pytest.raises(sizing.TokenizerUnavailable, match=r"Install giraffe-llm\[hf-tokenizer\]") as failed:
        await sizing.ensure_tokenizers(config)
    assert "pick Automatic" not in str(failed.value)


def test_cli_validate_imports_without_hf_extra(tmp_path):
    config = tmp_path / "giraffe.yaml"
    config.write_text("config_version: 2\ntargets:\n  - name: local\n"
                      "    url: http://localhost:8000/v1\n    model: model\nchecks: [correctness]\n")
    code = """
import sys
class BlockHF:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'transformers', 'huggingface_hub', 'jinja2'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, BlockHF())
from giraffe.cli import main
raise SystemExit(main(['validate', '--config', sys.argv[1]]))
"""
    result = subprocess.run([sys.executable, "-c", code, str(config)], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "Configuration is valid" in result.stdout


def test_composite_action_has_safe_inputs_and_always_publishes_results():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    assert set(action["inputs"]) == {"url", "model", "checks", "config", "baseline", "fail-on",
                                      "python-version", "version"}
    assert action["runs"]["using"] == "composite"
    steps = action["runs"]["steps"]
    assert any(step.get("uses", "").startswith("actions/setup-python@") for step in steps)
    for step in steps:
        if "run" in step:
            assert step["shell"] == "bash"
            assert "${{ inputs." not in step["run"]
            script = step["run"]
            assert script.startswith("python - <<'PY'\n")
            compile(script.split("\n", 1)[1].rsplit("\nPY", 1)[0], "action.yml", "exec")
    run = next(step for step in steps if step.get("id") == "run")
    assert "--junit" in run["run"] and "--summary-md" in run["run"]
    summary = next(step for step in steps if "GITHUB_STEP_SUMMARY" in step.get("run", ""))
    assert summary["if"] == "always()"
    upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["if"] == "always()"


def test_ci_keeps_existing_jobs_and_covers_python_311():
    checks = yaml.safe_load((ROOT / ".github/workflows/checks.yml").read_text())
    assert {"lint", "test", "test-python311"} <= checks["jobs"].keys()
    job = checks["jobs"]["test-python311"]
    assert any(step.get("with", {}).get("python-version") == "3.11" for step in job["steps"])
    assert any("--ignore-glob=tests/test_validation_*.py" in step.get("run", "") for step in job["steps"])
    for filename in ("checks.yml", "publish.yml"):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        installs = [step["run"] for job in workflow["jobs"].values() for step in job["steps"]
                    if "uv sync" in step.get("run", "") and "--extra dev" in step["run"]]
        assert installs and all("--extra hf-tokenizer" in command for command in installs)


def test_docker_runs_as_nonroot_with_writable_workdir():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "USER giraffe" in dockerfile
    assert "chown giraffe:giraffe /work" in dockerfile
    assert 'ENTRYPOINT ["giraffe"]' in dockerfile
