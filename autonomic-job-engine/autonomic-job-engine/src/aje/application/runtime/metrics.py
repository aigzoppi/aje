"""Sliding-window metrics used to build the Metrics fact."""

from __future__ import annotations

import collections
import math

import pydantic


class Completion(pydantic.BaseModel):
    t: float
    latency: float
    service: float
    ok: bool


class MetricsWindow:
    """Sliding window of completions, arrivals and utilisation samples over ``window`` seconds."""

    def __init__(self, window: float):
        self.window = window
        self.completions: collections.deque[Completion] = collections.deque()
        self.arrivals: collections.deque[tuple[float, int]] = collections.deque()
        self.samples: collections.deque[tuple[float, float, float]] = (
            collections.deque()
        )  # (t, live_workers, instant utilisation)

    def add_completion(self, c: Completion) -> None:
        self.completions.append(c)

    def add_arrivals(self, t: float, n: int) -> None:
        self.arrivals.append((t, n))

    def add_sample(self, t: float, live: float, util: float) -> None:
        self.samples.append((t, live, util))

    def prune(self, now: float) -> None:
        horizon = now - self.window
        while self.completions and self.completions[0].t < horizon:
            self.completions.popleft()
        for dq in (self.arrivals, self.samples):
            while dq and dq[0][0] < horizon:
                dq.popleft()

    def snapshot(self, now: float, elapsed: float) -> dict[str, float | None]:
        self.prune(now)
        span = max(1e-6, min(self.window, max(elapsed, 1.0)))
        ok = [c for c in self.completions if c.ok]
        lat = sorted(c.latency for c in ok)
        attempts = len(self.completions)
        fails = attempts - len(ok)
        busy_seconds = sum(min(c.service, span) for c in self.completions)
        avg_live = (sum(s[1] for s in self.samples) / len(self.samples)) if self.samples else 0.0
        service_util = busy_seconds / (avg_live * span) if avg_live > 0 else 0.0
        sample_util = (sum(s[2] for s in self.samples) / len(self.samples)) if self.samples else 0.0
        return {
            "throughput": round(len(ok) * 60.0 / span, 3),
            "arrival_rate": round(sum(n for _, n in self.arrivals) * 60.0 / span, 3),
            "avg_latency": round(sum(lat) / len(lat), 4) if lat else None,
            "p95_latency": round(lat[min(len(lat) - 1, math.ceil(0.95 * len(lat)) - 1)], 4) if lat else None,
            "avg_service_time": round(sum(c.service for c in ok) / len(ok), 4) if ok else None,
            "failure_rate": round(fails / attempts, 4) if attempts else 0.0,
            "avg_utilisation": round(min(1.0, max(service_util, sample_util)), 4),
        }
