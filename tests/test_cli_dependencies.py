import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from giraffe import cli


def test_required_module_check_matches_project_dependencies():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    imports = {"pyyaml": "yaml"}
    required = {
        imports.get(name, name.replace("-", "_"))
        for dependency in project["project"]["dependencies"]
        for name in [re.split(r"[<>=!~;\[]", dependency, maxsplit=1)[0].lower()]
    }
    assert set(cli.REQUIRED_MODULES) == required


@pytest.mark.parametrize("module", cli.REQUIRED_MODULES)
@pytest.mark.parametrize("arguments", [["--help"], ["describe"], ["ui"]])
def test_missing_required_module_exits_before_import_or_dispatch(module, arguments):
    script = """
import importlib.util
import sys

missing = sys.argv[1]
find_spec = importlib.util.find_spec
importlib.util.find_spec = lambda name: None if name == missing else find_spec(name)

class BlockMissingImport:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == missing:
            raise AssertionError('Imported the missing module before checking dependencies')

sys.meta_path.insert(0, BlockMissingImport())
from giraffe.cli import main
raise SystemExit(main(sys.argv[2:]))
"""
    result = subprocess.run([sys.executable, "-c", script, module, *arguments],
                            capture_output=True, text=True)
    assert result.returncode == 4
    assert result.stdout == ""
    assert result.stderr == (
        f"giraffe: missing required module '{module}'; "
        "run pip install --upgrade giraffe-llm\n"
    )


@pytest.mark.parametrize("module", cli.REQUIRED_MODULES)
def test_callable_main_checks_dependencies_before_help(module, monkeypatch, capsys):
    find_spec = cli.importlib.util.find_spec
    monkeypatch.setattr(cli.importlib.util, "find_spec",
                        lambda name: None if name == module else find_spec(name))
    monkeypatch.setattr(cli, "_main", lambda argv: pytest.fail("Dispatched a broken install"))
    assert cli.main(["--help"]) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"giraffe: missing required module '{module}'; "
        "run pip install --upgrade giraffe-llm\n"
    )


def test_dependency_check_does_not_import_tiktoken():
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; from giraffe.cli import main; "
         "assert 'tiktoken' not in sys.modules; "
         "main(['--help'])"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "usage: giraffe" in result.stdout
