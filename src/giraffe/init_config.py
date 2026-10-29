"""Small current-schema starter configurations, available in every wheel."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml

from giraffe.models import DEFAULT_CHECKS, RunConfig

ENGINES = {
    "ollama": (11434, "ollama"),
    "vllm": (8000, "vllm-v1"),
    "sglang": (30000, "sglang"),
    "llamacpp": (8080, "llamacpp"),
    "tgi": (8080, "tgi"),
    "lmdeploy": (23333, "lmdeploy"),
    "tensorrt-llm": (8000, "tensorrt-llm"),
}


def write_config(output: Path, *, engine: str, url: str | None = None, force: bool = False) -> None:
    if output.exists() and not force:
        raise ValueError(f"Configuration already exists: {output}; use --force to overwrite it")
    port, profile = ENGINES[engine]
    target = {"name": engine, "url": url or f"http://127.0.0.1:{port}/v1",
              "model": "replace-with-served-model-id", "metrics_profile": profile}
    values = {"config_version": 2, "targets": [target], "checks": list(DEFAULT_CHECKS),
              "max_requests": None, "default_concurrency": 4}
    # Validate before discovery so invalid URLs cannot create an unusable file.
    RunConfig.model_validate(values)
    if url:
        from giraffe.preflight import ModelDiscovery, PreflightError, discover_models
        try:
            discovered = asyncio.run(discover_models(ModelDiscovery(url=url), allow_manual=True))
            models = discovered["models"]
            if len(models) == 1:
                target["model"] = models[0]
            else:
                print("Note: set targets[].model to the served model ID; discovery did not return exactly one model.",
                      file=sys.stderr)
        except PreflightError:
            print("Warning: model discovery unavailable; set targets[].model before running.", file=sys.stderr)
    comments = ("# url: OpenAI-compatible API base for your running server.\n"
                "# model: exact served model ID; replace the placeholder before running.\n"
                "# checks: select the checks to run (giraffe describe lists all checks).\n"
                "# max_requests: total request budget; null leaves it unset.\n"
                "# default_concurrency: parallel requests per check; 1 runs serially.\n")
    with output.open("w" if force else "x") as file:
        file.write(comments + yaml.safe_dump(values, sort_keys=False))
