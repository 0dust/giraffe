"""Publishing must reject tags or commits that do not match the release source."""

import importlib.util
import subprocess
import tomllib
from pathlib import Path

import pytest

from giraffe import __version__


SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/validate_release.py"


def validator():
    spec = importlib.util.spec_from_file_location("validate_release", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_release


def test_current_repository_passes_release_validation():
    root = SCRIPT.parents[2]
    validate_release = validator()
    validate_release(root, "push", "x")


def test_package_version_matches_project_metadata():
    with (SCRIPT.parents[2] / "pyproject.toml").open("rb") as file:
        assert __version__ == tomllib.load(file)["project"]["version"]


def git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Release Test", "-c", "user.email=test@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def release_repo(tmp_path):
    (tmp_path / "src/giraffe").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0a1"\n')
    (tmp_path / "src/giraffe/__init__.py").write_text('__version__ = "0.1.0a1"\n')
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Initial release")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return tmp_path


def test_matching_release_on_main_is_valid(release_repo):
    validator()(release_repo, "release", "v0.1.0a1")


def test_release_tag_must_match_version(release_repo):
    with pytest.raises(ValueError, match="tag"):
        validator()(release_repo, "release", "v9.9.9")


def test_package_version_must_match_metadata(release_repo):
    (release_repo / "src/giraffe/__init__.py").write_text('__version__ = "0.2.0"\n')
    with pytest.raises(ValueError, match="versions"):
        validator()(release_repo, "release", "v0.1.0a1")


def test_release_commit_must_be_on_main(release_repo):
    git(release_repo, "checkout", "-b", "unmerged")
    (release_repo / "extra.txt").write_text("unmerged change\n")
    git(release_repo, "add", ".")
    git(release_repo, "commit", "-m", "Unmerged change")
    with pytest.raises(ValueError, match="origin/main"):
        validator()(release_repo, "release", "v0.1.0a1")


def test_pull_request_does_not_require_release_tag_or_main_ancestry(release_repo):
    git(release_repo, "commit", "--allow-empty", "-m", "PR change")
    validator()(release_repo, "pull_request", "123/merge")


def test_manual_testpypi_dispatch_requires_main(release_repo):
    with pytest.raises(ValueError, match="main"):
        validator()(release_repo, "workflow_dispatch", "feature")
