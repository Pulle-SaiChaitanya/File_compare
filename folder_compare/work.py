"""Cooperative cancellation and throttled worker progress."""
from __future__ import annotations

import time
from typing import Callable


class ComparisonCancelled(Exception):
    """The user cancelled; the current file must not be reported as complete."""


class Work:
    def __init__(
        self,
        cancelled: Callable[[], bool] | None = None,
        activity: Callable[[str], None] | None = None,
    ) -> None:
        self.cancelled = cancelled
        self.activity = activity
        self._last_update = 0.0
        self._stage = ""

    def check(self) -> None:
        if self.cancelled and self.cancelled():
            raise ComparisonCancelled()

    def update(self, stage: str, done: int = 0, total: int = 0) -> None:
        self.check()
        now = time.monotonic()
        if self.activity and (stage != self._stage or now - self._last_update >= 0.1):
            detail = f" ({done:,}/{total:,})" if total else (f" ({done:,})" if done else "")
            self.activity(stage + detail)
            self._last_update, self._stage = now, stage
