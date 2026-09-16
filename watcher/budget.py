"""Aggregate time budgets for the polling jobs (OTW-19).

A `Budget` is a wall-clock allowance for everything *one job* does — request
retries, per-listing loops, news feed loops, the Cinesa token refresh — rather
than a per-request timeout. That distinction is the point: Pathé retries every
request three times against a 20 s timeout over several requests, so a source
failing slowly in many small steps could burn a whole reminder window while
never exceeding any single timeout.

The source clients take `budget=None` and behave exactly as they always did,
so nothing outside `watcher/jobs.py` has to know budgets exist.

Lives apart from `jobs.py` so `pathe`/`news`/`cinesa` can import it without an
import cycle back through the jobs that call them.
"""

from __future__ import annotations

import time
from typing import Callable

# Never hand a client a timeout shorter than this: an almost-exhausted budget
# should let the request in flight finish rather than fail it on arrival.
MIN_REQUEST_TIMEOUT_SECONDS = 1.0

# A headed-Chrome token mint takes ~3 s when it works and has its own poll
# window when it does not. Below this much remaining budget a *proactive*
# refresh is skipped and the cached token kept: the refresh has many later
# firings to succeed, and a half-finished mint helps nobody.
TOKEN_MINT_BUDGET_SECONDS = 30.0


class Budget:
    """An aggregate wall-clock allowance for one bounded job.

    `monotonic` and `sleep` are injectable so tests can drive a job through a
    long, entirely fake outage without waiting for one.
    """

    def __init__(
        self,
        seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        label: str = "job",
    ) -> None:
        self.label = label
        self.seconds = max(0.0, float(seconds))
        self._monotonic = monotonic
        self._sleep = sleep
        self._started = monotonic()

    def elapsed(self) -> float:
        return max(0.0, self._monotonic() - self._started)

    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def allows(self, seconds: float) -> bool:
        """Whether there is room left for a step known to cost about `seconds`."""
        return self.remaining() >= seconds

    def timeout(self, ceiling: float) -> float:
        """A per-request timeout that cannot outlive the budget.

        Floored at MIN_REQUEST_TIMEOUT_SECONDS, so a job may overshoot by up to
        a second — which is deliberate: the alternative is failing a request
        the instant it starts.
        """
        return max(MIN_REQUEST_TIMEOUT_SECONDS, min(float(ceiling), self.remaining()))

    def sleep(self, seconds: float) -> None:
        """A politeness or backoff pause, clipped to what is left."""
        nap = min(float(seconds), self.remaining())
        if nap > 0:
            self._sleep(nap)

    def child(self, seconds: float, label: str) -> Budget:
        """A sub-budget bounded by both its own share and this one's remainder."""
        return Budget(
            min(float(seconds), self.remaining()),
            monotonic=self._monotonic,
            sleep=self._sleep,
            label=label,
        )

    def exhausted_message(self) -> str:
        """Stable, URL-free diagnostic. It is stored in `last_error` for the
        cloud pass to read back, so it must not churn between endpoints."""
        return f"{self.label} exceeded its {self.seconds:.0f}s time budget"


def out_of_time(budget: Budget | None) -> bool:
    return budget is not None and budget.expired()


def request_timeout(budget: Budget | None, ceiling: float) -> float:
    return ceiling if budget is None else budget.timeout(ceiling)
