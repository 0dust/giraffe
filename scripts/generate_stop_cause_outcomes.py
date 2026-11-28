"""Generate normalized golden outcomes using an explicitly supplied old runner.

See tests/fixtures/stop_cause_outcomes.md for the pinned revision and command.
Normal tests never load this generator or require Git.
"""

from pathlib import Path
import sys
import tempfile

import pytest


def main():
    runner_path = Path(sys.argv[1]).resolve()
    destination = Path(sys.argv[2]).resolve()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    with tempfile.TemporaryDirectory(prefix="stop-cause-golden-") as directory:
        path = Path(directory)
        (path / "conftest.py").write_text("from tests.conftest import *  # noqa\n")
        (path / "test_generate.py").write_text('''
import importlib.util
import json
import sys

import pytest

from tests.test_runner import client as client
from tests.test_stop_cause_bytes import SCENARIOS, scenario_outcome

outcomes = {}


@pytest.fixture(scope="module")
def original_runner():
    name = "giraffe._golden_runner"
    spec = importlib.util.spec_from_file_location(name, ''' + repr(str(runner_path)) + ''')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules[name]
    assert set(outcomes) == set(SCENARIOS)
    from pathlib import Path
    Path(''' + repr(str(destination)) + ''').write_text(json.dumps(outcomes, indent=2, sort_keys=True) + "\\n")


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_generate(client, monkeypatch, original_runner, scenario):
    outcomes[scenario] = json.loads(await scenario_outcome(original_runner, monkeypatch, scenario))
''')
        return pytest.main(["-q", "-p", "no:cacheprovider", "-o", "asyncio_mode=auto", str(path)])


if __name__ == "__main__":
    raise SystemExit(main())
