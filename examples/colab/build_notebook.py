"""Bundle this checkout and a 17-check configuration into a self-contained Colab notebook."""

import argparse
import ast
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile

import yaml

from giraffe.models import CHECK_NAMES, RunConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--engine", choices=["llamacpp", "lmdeploy", "tgi", "tensorrt-llm"],
                        default="lmdeploy")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    config = RunConfig.model_validate(yaml.safe_load(
        (args.config or repo / "examples/lmdeploy.yaml").read_text()))
    if set(config.checks) != set(CHECK_NAMES) or len(config.targets) != 1:
        raise ValueError("Colab validation needs exactly one target and all 17 checks")
    values = config.model_dump(mode="json")
    # Uploaded configuration contains no workstation-specific paths or secret refs.
    values["targets"] = [{"name": "engine-1b", "url": "http://127.0.0.1:18080/v1", "model": "placeholder"}]
    if config.custom_fixtures or config.request_pack or config.judging.judges or config.restart_target:
        raise ValueError("Colab bundle accepts built-in fixtures without judges or restart commands")
    for options in values["test_options"].values():
        options.pop("server_log", None)
        options.pop("trace_file", None)
    values.update(ca_bundle=None, proxy=None)
    bundle = io.BytesIO()
    source_paths = sorted(set(subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "src"],
        cwd=repo, text=True,
    ).splitlines()))
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in [*source_paths, "pyproject.toml", "LICENSE", "README.md"]:
            archive.writestr("source/" + relative, (repo / relative).read_bytes())
        archive.writestr("run_engine.py", (repo / "examples/colab/run_engine.py").read_bytes())
        archive.writestr("config.json", json.dumps(values, indent=2))
    payload = bundle.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode()
    intro = """# Giraffe: one 1B engine, all 17 checks

Select a GPU runtime and **Run all**. Use one engine at a time; download its results
and terminate the runtime before starting the next. TensorRT-LLM 1.2.1 needs an
Ampere-or-newer GPU (A100/L4); T4 is rejected before model loading.

This notebook embeds the current Giraffe source and runs the server and suite over
localhost. It stops the server and downloads the raw report, hardware/model
provenance, installation log and server log. Installation/startup failures remain
inconclusive. TGI uses its unoptimized source path. Missing GPU telemetry and native
cancellation proof remain unknown. See `examples/colab/README.md` for the recipe.
"""
    setup = (
        f'ENGINE = {json.dumps(args.engine)} #@param ["llamacpp", "lmdeploy", "tgi", "tensorrt-llm"]\n'
        "import base64, hashlib, io, zipfile\nfrom pathlib import Path\n"
        f"payload = base64.b64decode({encoded!r})\n"
        f"assert hashlib.sha256(payload).hexdigest() == {digest!r}\n"
        "root = Path('/content/giraffe-engine-validation')\nroot.mkdir(parents=True, exist_ok=True)\n"
        "with zipfile.ZipFile(io.BytesIO(payload)) as bundle:\n    bundle.extractall(root)\n"
        f"print('Bundled source SHA256:', {digest!r})\nprint('Selected engine:', ENGINE)\n"
    )
    run = (
        "import subprocess, sys\n"
        "with subprocess.Popen([sys.executable, '-u', str(root / 'run_engine.py'), ENGINE],\n"
        "        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as job:\n"
        "    for line in job.stdout:\n        print(line, end='', flush=True)\n"
        "    if job.wait():\n        raise RuntimeError('Engine runner failed; inspect the output.')\n"
    )
    download = (
        "from google.colab import files\n"
        "archives = sorted(root.glob(ENGINE + '-*.zip'), key=lambda p: p.stat().st_mtime)\n"
        "if not archives:\n    raise RuntimeError('No result archive; inspect the previous cell.')\n"
        "files.download(str(archives[-1]))\n"
    )

    def cell(kind, text):
        result = {"cell_type": kind, "metadata": {}, "source": text.splitlines(keepends=True)}
        if kind == "code":
            ast.parse(text)
            result.update(execution_count=None, outputs=[])
        return result

    notebook = {"nbformat": 4, "nbformat_minor": 5,
                "metadata": {"accelerator": "GPU", "colab": {"name": args.output.name},
                             "kernelspec": {"display_name": "Python 3", "name": "python3"}},
                "cells": [cell("markdown", intro), cell("code", setup), cell("code", run), cell("code", download)]}
    notebook["cells"][1]["metadata"]["cellView"] = "form"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(notebook, indent=1) + "\n")
    print(json.dumps({"path": str(args.output.resolve()), "bundle_sha256": digest,
                      "bytes": args.output.stat().st_size, "checks": len(config.checks)}))


if __name__ == "__main__":
    main()
