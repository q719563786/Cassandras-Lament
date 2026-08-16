"""Synchronization for user-visible long-running operations."""

from __future__ import annotations

import threading
import time


class OperationBusy(RuntimeError):
    """Raised when a second cognition run overlaps an active run."""


class CognitionOperation:
    """Allow only one cognition pass across every application entry point."""

    def __init__(self, controller) -> None:
        self.controller = controller
        self._lock = threading.Lock()
        self._started_at = None  # monotonic seconds when the current run began

    @property
    def running(self) -> bool:
        """True while a cognition pass holds the lock."""
        return self._lock.locked()

    @property
    def started_at_monotonic(self) -> float | None:
        """Monotonic seconds when the current run began, or None when idle."""
        return self._started_at

    def run(self, source: str = "manual") -> dict:
        if not self._lock.acquire(blocking=False):
            raise OperationBusy("认知任务正在运行")
        self._started_at = time.monotonic()
        started = self._started_at
        try:
            result = self.controller.process_once()
            return {
                **result,
                "source": source,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        finally:
            self._lock.release()
            self._started_at = None
