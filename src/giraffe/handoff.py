"""Portable, copyable CLI commands for an already configured run."""

from __future__ import annotations

import shlex

from giraffe.models import RunConfig, RunReport


def _delimiter(label: str, payload: str) -> str:
    """Keep data from ever closing its own quoted here-document."""
    base = f"GIRAFFE_{label}_EOF"
    delimiter = base
    lines = set(payload.splitlines())
    suffix = 0
    while delimiter in lines:
        suffix += 1
        delimiter = f"{base}_{suffix}"
    return delimiter


def cli_command(
    config: RunConfig,
    *,
    mode: str = "normal",
    restart_target: str | None = None,
    baseline: RunReport | None = None,
) -> str:
    """Return a POSIX shell snippet; generating it creates no files or processes."""
    if mode not in {"normal", "cold-start"}:
        raise ValueError("Unsupported run mode")
    if mode == "normal":
        if restart_target is not None or config.restart_target is not None:
            raise ValueError("Restart requires explicit cold-start mode and target")
        command = "giraffe run"
    else:
        target = next((target for target in config.targets if target.name == restart_target), None)
        if target is None or not target.restart_command:
            raise ValueError("Cold start requires a named target with a restart command")
        if "\x00" in target.name:
            raise ValueError("Restart target must not contain a NUL character")
        if config.restart_target not in {None, restart_target}:
            raise ValueError("Restart targets must match")
        command = "giraffe cold-start --restart=" + shlex.quote(target.name)
    command += " --config - --output-format json"
    config_json = config.model_dump_json(indent=2)
    config_delimiter = _delimiter("CONFIG", config_json)
    if baseline is None:
        return f"{command} <<'{config_delimiter}'\n{config_json}\n{config_delimiter}\n"

    baseline_json = baseline.model_dump_json(indent=2)
    baseline_delimiter = _delimiter("BASELINE", baseline_json)
    return (
        "(\n"
        '  giraffe_baseline=$(mktemp "${TMPDIR:-/tmp}/giraffe-baseline.XXXXXX") || exit 1\n'
        "  trap 'giraffe_status=$?; rm -f -- \"$giraffe_baseline\"; "
        "exit \"$giraffe_status\"' 0\n"
        f'  cat >"$giraffe_baseline" <<\'{baseline_delimiter}\' || exit 1\n'
        f"{baseline_json}\n{baseline_delimiter}\n"
        f'  {command} --baseline "$giraffe_baseline" <<\'{config_delimiter}\'\n'
        f"{config_json}\n{config_delimiter}\n"
        ")\n"
    )
