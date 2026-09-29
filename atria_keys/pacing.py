"""Single-lane pacing: every run shares one exit IP, so runs are spaced by
a randomized inter-run interval, capped per day, and consecutive challenge
failures escalate a cooldown instead of hammering the widget."""

from __future__ import annotations

import logging
import random
import time
from typing import Callable

from .errors import PacingHalt

log = logging.getLogger("atria_keys.pacing")


class Pacer:
    def __init__(
        self,
        state,
        min_interval_s: float,
        max_interval_s: float,
        jitter_s: float,
        max_runs_per_day: int,
        challenge_backoff_s: float,
        challenge_backoff_multipliers: list[float],
        max_consecutive_challenge_failures: int,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ):
        self.state = state
        self.min_s = min_interval_s
        self.max_s = max_interval_s
        self.jitter_s = jitter_s
        self.max_per_day = max_runs_per_day
        self.backoff_s = challenge_backoff_s
        self.multipliers = challenge_backoff_multipliers or [1, 2, 4, 8]
        self.max_consec = max_consecutive_challenge_failures
        self._sleep = sleep
        self._now = now
        self._rng = rng or random.Random()
        self.consecutive_challenge_failures = 0

    def required_gap(self) -> float:
        return self._rng.uniform(self.min_s, self.max_s) + self._rng.uniform(
            0, self.jitter_s
        )

    def wait_for_slot(self, run_id: str) -> float:
        """Block until the next run may start; returns the waited seconds."""
        if self.state.runs_today() >= self.max_per_day:
            raise PacingHalt(f"daily cap reached ({self.max_per_day} runs/day)")
        last = self.state.last_run_start_ts()
        gap = self.required_gap()
        waited = 0.0
        if last is not None:
            elapsed = self._now() - last
            if elapsed < gap:
                waited = gap - elapsed
                log.info("pacing: waiting %.1fs before run %s", waited, run_id)
                self._sleep(waited)
        return waited

    def on_challenge_failure(self) -> float:
        """Escalating cooldown after a failed challenge attempt; raises
        PacingHalt when the consecutive-failure breaker trips."""
        self.consecutive_challenge_failures += 1
        if self.consecutive_challenge_failures >= self.max_consec:
            raise PacingHalt(
                f"{self.consecutive_challenge_failures} consecutive challenge "
                "failures — circuit breaker open"
            )
        idx = min(self.consecutive_challenge_failures - 1, len(self.multipliers) - 1)
        wait = self.backoff_s * self.multipliers[idx] * self._rng.uniform(0.8, 1.2)
        log.warning(
            "challenge failure #%d: cooling down %.1fs",
            self.consecutive_challenge_failures,
            wait,
        )
        self._sleep(wait)
        return wait

    def on_challenge_success(self) -> None:
        self.consecutive_challenge_failures = 0
