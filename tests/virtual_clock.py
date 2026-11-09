"""Deterministic asyncio time for mocked transports; never use with real sockets."""

import asyncio
from importlib import import_module
import math
import sys
import time
from types import SimpleNamespace

from giraffe import runner


class VirtualLoop(asyncio.SelectorEventLoop):
    """Run the real scheduling/batching code without spending seconds asleep."""

    now = 0
    _executor_pending = 0

    def time(self):
        return self.now

    def run_in_executor(self, executor, func, *args):
        future = super().run_in_executor(executor, func, *args)
        self._executor_pending += 1

        def completed(_):
            self._executor_pending -= 1

        future.add_done_callback(completed)
        return future

    def _run_once(self):
        if not self._ready and self._scheduled and not self._executor_pending:
            self.now = max(self.now, min((handle._when for handle in self._scheduled
                                          if not handle.cancelled()), default=self.now))
        # asyncio runs timers strictly before now + resolution. On the
        # virtual clock, even the next representable time is still future.
        self._clock_resolution = math.ulp(self.now)
        super()._run_once()


def run_virtual(monkeypatch, coroutine):
    """Keep elapsed records and runner deadlines on the same deterministic clock."""
    async def no_exporter_network(run, target, phase):
        """Synthetic-clock runs measure mocked inference, with no real exporter I/O."""
        run.observations[target.name]["runtime_metrics_discovery"] = {
            "status": "unavailable", "reason": "No runtime exporter in this deterministic fixture."}

    monkeypatch.setattr(runner, "_metrics", no_exporter_network)
    for name in ("client", "traffic", "workloads", "concurrent_load", "deployment", "telemetry", "timing"):
        import_module("giraffe." + name)
    with asyncio.Runner(loop_factory=VirtualLoop) as execution:
        loop = execution.get_loop()
        clock = SimpleNamespace(**{name: getattr(time, name) for name in dir(time)
                                   if not name.startswith("_")})
        clock.monotonic = clock.perf_counter = loop.time
        clock.monotonic_ns = clock.perf_counter_ns = lambda: int(loop.time() * 1e9)
        for name, module in list(sys.modules.items()):
            if ((name.startswith("giraffe.") and name != "giraffe.resources")
                    or name == "tests.test_runner") and getattr(module, "time", None) is time:
                monkeypatch.setattr(module, "time", clock)
            if name.startswith("giraffe.") and getattr(module, "monotonic", None) is time.monotonic:
                monkeypatch.setattr(module, "monotonic", loop.time)
        return execution.run(coroutine)
