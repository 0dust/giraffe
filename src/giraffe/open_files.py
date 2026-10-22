"""Estimate and, at run start only, raise the process's open-file allowance."""

from __future__ import annotations

import math
import os
import subprocess
import sys
from functools import lru_cache

from giraffe.models import CATALOGUE_NAMES, required_concurrency

try:
    import resource
except ImportError:  # Windows has no resource module.
    resource = None


def _open_descriptors():
    paths = ("/dev/fd", "/proc/self/fd") if sys.platform == "darwin" else ("/proc/self/fd", "/dev/fd")
    for path in paths:
        try:
            return len(os.listdir(path))
        except OSError:
            pass
    return None


def _macos_cap():
    try:
        value = int(subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.maxfilesperproc"],
                                          stderr=subprocess.DEVNULL, timeout=1))
        if value > 0:
            return value
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    # SC_OPEN_MAX reports the current soft limit on macOS, not the kernel cap.
    # Using it here would prevent raising the limit after sysctl is unavailable.
    return 10240


def _finite_limit(value):
    return math.inf if value == resource.RLIM_INFINITY else value


@lru_cache(maxsize=1)
def _preview_macos_cap():
    return _macos_cap()


def open_file_guard(config, *, raise_limit=False, web=False):
    """Return preview issues or attempt the same requirement at actual run start.

    ``raise_limit`` must stay false for previews and Copy CLI. Overlapping clients
    can retain idle connections after another endpoint acquires their run slots.
    """
    peak = required_concurrency(config)
    pool = min(peak["peak"], config.concurrency) if config.concurrency is not None else peak["peak"]
    if config.overlap_models:
        keepalive = min(peak["per_target_peak"], pool) * len(config.targets)
    else:
        keepalive = min(peak["per_target_peak"], pool)
    target_sockets = max(pool, keepalive)
    from giraffe.request_pack import active_pack

    judging_active = (active_pack(config) and bool(config.judging.judges)
                      and any(case.scorer == "judge" for case in config.request_pack.cases))
    judging = config.judging.concurrency if judging_active else 0
    metrics = sum(bool(target.metrics_url) for target in config.targets) if config.metrics or "gpu" in config.checks else 0
    deployment = sum(target.deployment.discovery != "none" or bool(target.deployment.collector_url)
                     for target in config.targets)
    fields = ["concurrency", "default_concurrency"]
    for check, value in peak["by_check"].items():
        if value == peak["per_target_peak"]:
            fields.append({"capacity": "traffic.max_in_flight", "arrivals": "arrivals.max_in_flight",
                           "sessions": "sessions.concurrency", "consistency": "consistency.levels",
                           "concurrency": "concurrent_load.schedule.levels", "load": "load_test.patterns.concurrent.levels"}.get(check,
                          f"test_options.{check}.concurrency" if check in {"correctness", "json", "recovery"}
                          else "checks"))
    fields = list(dict.fromkeys(fields))
    contributors = [
        {"label": "target sockets", "count": target_sockets, "fields": fields,
         "checks": [CATALOGUE_NAMES[check] for check, value in peak["by_check"].items() if value == peak["per_target_peak"]]},
        {"label": "AI judging sockets", "count": judging, "fields": ["judging.concurrency"]},
        {"label": "metrics clients", "count": metrics, "fields": ["metrics"]},
        {"label": "deployment clients", "count": deployment, "fields": ["targets"]},
    ]
    contributors = [item for item in contributors if item["count"] > 0]
    result = {"supported": resource is not None, "need": None, "before": None, "after": None,
              "effective_hard": None, "counts": {"slot_pool": pool, "keepalive_bound": keepalive,
                  "target_sockets": target_sockets, "judging_sockets": judging,
                  "socket_peak": max(target_sockets, judging), "metrics_clients": metrics,
                  "deployment_clients": deployment, "open_descriptors": None, "margin": 32},
              "contributors": contributors, "errors": [], "warnings": []}
    if resource is None:
        result["warnings"].append({"fields": [], "values": {},
            "msg": "Open-file guard skipped: resource limits are unavailable on this platform."})
        return result
    opened = _open_descriptors()
    if opened is None:
        result["supported"] = False
        result["warnings"].append({"fields": [], "values": {},
            "msg": "Open-file guard skipped: current descriptors could not be measured from /dev/fd or /proc/self/fd."})
        return result
    result["counts"]["open_descriptors"] = opened
    need = max(target_sockets, judging) + metrics + deployment + opened + 32
    result["need"] = need
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    result["before"] = {"soft": soft, "hard": hard}
    if raise_limit:
        _preview_macos_cap.cache_clear()
    macos_cap = (_macos_cap() if raise_limit else _preview_macos_cap()) if sys.platform == "darwin" else math.inf
    cap = min(_finite_limit(hard), macos_cap)
    result["effective_hard"] = None if math.isinf(cap) else cap
    failure = None
    if raise_limit and _finite_limit(soft) < need:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(need, cap), hard))
        except (OSError, ValueError) as exc:
            failure = type(exc).__name__
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    result["after"] = {"soft": soft, "hard": hard}
    if _finite_limit(soft) >= need:
        return result
    largest = sorted(contributors, key=lambda item: item["count"], reverse=True)[:2]
    issue_fields = list(dict.fromkeys(field for item in largest for field in item["fields"]))
    values = {"required": need, "soft_limit": soft, "hard_limit": hard,
              "effective_hard_limit": result["effective_hard"], **result["counts"]}
    details = (f"{max(target_sockets, judging):,} socket peak (target pool {pool:,}, keepalive bound {keepalive:,}, "
               f"AI judging {judging:,}) + {opened:,} open descriptors + {metrics:,} metrics clients + "
               f"{deployment:,} deployment clients + 32 margin")
    message = f"This run needs {need:,} open files ({details}); the current soft limit is {soft:,} and hard limit is {hard:,}."
    issue = {"fields": issue_fields, "values": values,
             "actions": [{"label": f"Edit {item['label']}", "fields": item["fields"]} for item in largest]}
    if raise_limit or cap < need:
        message += " Reduce the largest contributors or run `ulimit -n 4096` in the shell."
        if web:
            message += " Restart `giraffe web` from that shell."
        if failure:
            message += f" Raising the soft limit failed ({failure})."
        result["errors"].append(issue | {"msg": message})
    else:
        result["warnings"].append(issue | {"msg": message + " The soft limit will be raised automatically when the run starts."})
    return result
