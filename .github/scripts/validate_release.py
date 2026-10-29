"""Validate release identity before building any publishable distributions."""

import ast
import os
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
    if event_name == "workflow_dispatch" and ref_name != "main":
        raise ValueError("TestPyPI dispatch must use main")
    if event_name == "release":
        if ref_name != f"v{version}":
            raise ValueError(f"Release tag must be v{version}")
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/main"],
            cwd=root,
            check=False,
        )
        if ancestry.returncode != 0:
            raise ValueError("Release commit must be reachable from origin/main")


if __name__ == "__main__":
    validate_release(Path.cwd(), os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF_NAME"])
