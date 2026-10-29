"""Explicit measurement time for integration tests whose contract is not host speed."""

import time
from types import SimpleNamespace


class MeasurementClock:
    def __init__(self, monkeypatch):
        from giraffe import client, runner

        self.now = time.monotonic()
        self.monitors = []
        monitors = self.monitors
        original = runner.TimingHealth

        class ClockedHealth(original):
            def __init__(self, start):
                super().__init__(start)
                monitors.append(self)

        # Replace only these modules' clock references. Asyncio, HTTPX and the
        # test watchdog retain their real clocks and cancellation behavior.
        monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=self.monotonic))
        monkeypatch.setattr(runner, "TimingHealth", ClockedHealth)
        monkeypatch.setattr(client, "monotonic", self.monotonic)
        monkeypatch.setattr(client, "time", SimpleNamespace(
            perf_counter=self.read, monotonic=self.monotonic,
            time_ns=time.time_ns, monotonic_ns=time.monotonic_ns))

    def monotonic(self):
        return self.now

    def read(self):
        self.advance(.001)
        return self.now

    def advance(self, seconds, *, responsive=True):
        end = self.now + seconds
        if responsive:
            # Model a responsive client during elapsed endpoint time. Exercise
            # the real health sampler; an explicit stall skips these heartbeats.
            while self.now < end:
                self.now = min(end, self.now + .025)
                for monitor in self.monitors:
                    monitor.sample(self.now)
        else:
            self.now = end
