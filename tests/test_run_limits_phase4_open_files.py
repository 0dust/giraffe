from types import SimpleNamespace

import pytest

from giraffe import open_files
from giraffe.models import RunConfig
from giraffe.runner import _Run


def config(**values):
    return RunConfig(targets=[{"name": "one", "url": "http://local", "model": "m"}],
                     checks=["correctness"], **values)


@pytest.fixture
def limits(monkeypatch):
    state = {"soft": 64, "hard": 4096, "calls": []}

    def setrlimit(kind, values):
        assert kind == 1
        state["calls"].append(values)
        state["soft"], state["hard"] = values

    monkeypatch.setattr(open_files, "resource", SimpleNamespace(RLIMIT_NOFILE=1, RLIM_INFINITY=-1,
                        getrlimit=lambda kind: (state["soft"], state["hard"]), setrlimit=setrlimit))
    monkeypatch.setattr(open_files, "_open_descriptors", lambda: 12)
    monkeypatch.setattr(open_files.sys, "platform", "linux")
    return state


def test_preview_warns_without_raising_and_start_raises_first(limits):
    cfg = config(default_concurrency=40)
    preview = open_files.open_file_guard(cfg)
    assert preview["need"] == 84
    assert not preview["errors"]
    assert "raised automatically" in preview["warnings"][0]["msg"]
    assert limits["calls"] == []
    started = open_files.open_file_guard(cfg, raise_limit=True)
    assert started["before"] == {"soft": 64, "hard": 4096}
    assert started["after"] == {"soft": 84, "hard": 4096}
    assert not started["warnings"] and not started["errors"]
    assert limits["calls"] == [(84, 4096)]


def test_hard_limit_insufficient_blocks_with_counts_and_web_remedy(limits):
    limits["hard"] = 70
    cfg = config(default_concurrency=40)
    preview = open_files.open_file_guard(cfg, web=True)
    assert preview["errors"] and limits["calls"] == []
    result = open_files.open_file_guard(cfg, raise_limit=True, web=True)
    assert limits["calls"] == [(70, 70)]
    issue = result["errors"][0]
    assert issue["values"]["required"] == 84
    assert issue["values"]["soft_limit"] == 70
    assert "84 open files" in issue["msg"]
    assert "40 socket peak" in issue["msg"]
    assert "12 open descriptors" in issue["msg"]
    assert "32 margin" in issue["msg"]
    assert "ulimit -n 4096" in issue["msg"]
    assert "Restart `giraffe web` from that shell" in issue["msg"]
    assert "default_concurrency" in issue["fields"]
    assert issue["actions"][0]["fields"]


def test_failed_raise_blocks_only_after_attempt(limits, monkeypatch):
    def rejected(kind, values):
        limits["calls"].append(values)
        raise OSError("denied")

    monkeypatch.setattr(open_files.resource, "setrlimit", rejected)
    result = open_files.open_file_guard(config(default_concurrency=40), raise_limit=True)
    assert limits["calls"] == [(84, 4096)]
    assert result["after"]["soft"] == 64
    assert "Raising the soft limit failed (OSError)" in result["errors"][0]["msg"]


def test_judging_dominates_without_adding_target_pool(limits):
    result = open_files.open_file_guard(config(request_source="request_pack",
        request_pack={"version": 1, "cases": [{"fixture_id": "one", "scorer": "judge",
            "expected": "answer", "messages": [{"role": "user", "content": "Question"}]}]},
        judging={"concurrency": 100, "judges": [{"name": "judge", "url": "http://judge", "model": "m"}]}))
    assert result["counts"]["slot_pool"] == 4
    assert result["counts"]["socket_peak"] == 100
    assert result["need"] == 144
    assert result["warnings"][0]["actions"][0] == {
        "label": "Edit AI judging sockets", "fields": ["judging.concurrency"]}


def test_metrics_and_discovery_clients_follow_enabled_runtime_paths(limits):
    targets = [{"name": "one", "url": "http://local", "model": "m", "metrics_url": "http://metrics",
                "deployment": {"discovery": "vllm"}},
               {"name": "two", "url": "http://other", "model": "m",
                "deployment": {"collector_url": "http://collector"}}]
    cfg = RunConfig(targets=targets, checks=["correctness", "gpu"])
    result = open_files.open_file_guard(cfg)
    assert result["counts"]["metrics_clients"] == 1
    assert result["counts"]["deployment_clients"] == 2
    assert result["need"] == 4 + 12 + 1 + 2 + 32
    cfg.metrics = False
    cfg.checks.remove("gpu")
    assert open_files.open_file_guard(cfg)["counts"]["metrics_clients"] == 0
    gpu = RunConfig(targets=targets, checks=["gpu"])
    assert open_files.open_file_guard(gpu)["counts"]["metrics_clients"] == 1


@pytest.mark.parametrize("overlap,ceiling", [(False, None), (True, None), (True, 1), (True, 5)])
def test_socket_bound_matches_runtime_pool_and_client_shares(limits, overlap, ceiling):
    cfg = RunConfig(targets=[{"name": name, "url": "http://local", "model": "m"} for name in ("a", "b", "c")],
                    checks=["correctness"], overlap_models=overlap, concurrency=ceiling)
    run = _Run(cfg, None, None)
    result = open_files.open_file_guard(cfg)
    assert result["counts"]["slot_pool"] == run.slots.maxsize
    shares = sum(run.connection_limits.values()) if overlap else max(run.connection_limits.values())
    assert result["counts"]["keepalive_bound"] == shares
    assert result["counts"]["target_sockets"] == max(run.slots.maxsize, shares)
    if overlap and ceiling == 1:
        assert result["counts"]["target_sockets"] == 3


def test_macos_kernel_cap_is_used_and_never_sets_infinity(limits, monkeypatch):
    monkeypatch.setattr(open_files.sys, "platform", "darwin")
    monkeypatch.setattr(open_files, "_macos_cap", lambda: 80)
    limits["hard"] = -1
    result = open_files.open_file_guard(config(default_concurrency=40), raise_limit=True)
    assert result["effective_hard"] == 80
    assert limits["calls"] == [(80, -1)]
    assert result["errors"]
    monkeypatch.setattr(open_files, "_macos_cap", lambda: 1000)
    result = open_files.open_file_guard(config(default_concurrency=40), raise_limit=True)
    assert limits["calls"][-1] == (84, -1)
    assert not result["errors"]


def test_macos_cap_prefers_kernel_over_current_soft_open_max(monkeypatch):
    monkeypatch.setattr(open_files.subprocess, "check_output", lambda *a, **kw: b"10240\n")
    monkeypatch.setattr(open_files.os, "sysconf", lambda key: 256)
    assert open_files._macos_cap() == 10240


def test_macos_open_max_fallback(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("no sysctl")

    monkeypatch.setattr(open_files.subprocess, "check_output", unavailable)
    monkeypatch.setattr(open_files.os, "sysconf", lambda key: 1024)
    assert open_files._macos_cap() == 10240


def test_unlimited_soft_limit_needs_no_raise(limits):
    limits.update(soft=-1, hard=-1)
    result = open_files.open_file_guard(config(default_concurrency=40), raise_limit=True)
    assert limits["calls"] == []
    assert not result["errors"] and not result["warnings"]


def test_unsupported_resource_skips_with_note(monkeypatch):
    monkeypatch.setattr(open_files, "resource", None)
    result = open_files.open_file_guard(config(), raise_limit=True)
    assert not result["supported"]
    assert result["need"] is None
    assert not result["errors"]
    assert "resource limits are unavailable" in result["warnings"][0]["msg"]


def test_macos_preview_caches_cap_and_start_refreshes_it(limits, monkeypatch):
    calls = []
    monkeypatch.setattr(open_files.sys, "platform", "darwin")
    monkeypatch.setattr(open_files, "_macos_cap", lambda: calls.append("cap") or 4096)
    open_files._preview_macos_cap.cache_clear()
    try:
        cfg = config(default_concurrency=40)
        open_files.open_file_guard(cfg)
        open_files.open_file_guard(cfg)
        assert calls == ["cap"]
        assert limits["calls"] == []
        open_files.open_file_guard(cfg, raise_limit=True)
        assert calls == ["cap", "cap"]
        assert limits["calls"] == [(84, 4096)]
        open_files.open_file_guard(cfg)
        assert calls == ["cap", "cap", "cap"]
    finally:
        open_files._preview_macos_cap.cache_clear()


def test_fd_measurement_fallback(monkeypatch):
    monkeypatch.setattr(open_files.sys, "platform", "linux")
    paths = []

    def listdir(path):
        paths.append(path)
        if path == "/proc/self/fd":
            raise OSError("unavailable")
        return ["0", "1", "2"]

    monkeypatch.setattr(open_files.os, "listdir", listdir)
    assert open_files._open_descriptors() == 3
    assert paths == ["/proc/self/fd", "/dev/fd"]


def test_unmeasurable_descriptors_skip_with_note(monkeypatch, limits):
    monkeypatch.setattr(open_files, "_open_descriptors", lambda: None)
    result = open_files.open_file_guard(config(), raise_limit=True)
    assert not result["supported"] and not result["errors"]
    assert "could not be measured" in result["warnings"][0]["msg"]
    assert limits["calls"] == []


def test_host_exporter_is_budgeted_even_without_runtime_metrics(limits):
    cfg = config()
    before = open_files.open_file_guard(cfg)
    cfg.targets[0].host_metrics_url = "http://host/metrics"
    after = open_files.open_file_guard(cfg)
    assert not cfg.metrics
    assert after["counts"]["metrics_clients"] == before["counts"]["metrics_clients"] + 1
    assert after["need"] == before["need"] + 1
