"""Validate release identity before building any publishable distributions."""

import ast
import os
import re
import subprocess
import tomllib
from pathlib import Path


def validate_release(root: Path, event_name: str, ref_name: str) -> None:
    with (root / "pyproject.toml").open("rb") as file:
        version = tomllib.load(file)["project"]["version"]
    module = ast.parse((root / "src/giraffe/__init__.py").read_text())
    package_version = next(
        (
            ast.literal_eval(node.value)
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__version__"
                    for target in node.targets)
        ),
        None,
    )
    if version != package_version:
        raise ValueError("pyproject.toml and giraffe.__version__ versions must match")
    version_line = re.match(r"^(\d+)\.(\d+)\.", version)
    if version_line is None:
        raise ValueError("Release version must include major.minor.patch")
    release_branch = f"release/{version_line[1]}.{version_line[2]}"
    if event_name == "workflow_dispatch" and ref_name not in ("main", release_branch):
        raise ValueError(f"TestPyPI dispatch must use main or {release_branch}")
    if event_name == "release":
        if ref_name != f"v{version}":
            raise ValueError(f"Release tag must be v{version}")
        for branch in ("main", release_branch):
            ancestry = subprocess.run(
                ["git", "merge-base", "--is-ancestor", "HEAD", f"refs/remotes/origin/{branch}"],
                cwd=root,
                check=False,
                capture_output=True,
            )
            if ancestry.returncode == 0:
                break
        else:
            raise ValueError(
                f"Release commit must be reachable from origin/main or origin/{release_branch}"
            )


if __name__ == "__main__":
    validate_release(Path.cwd(), os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF_NAME"])
