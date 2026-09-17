"""Heartbeat reaper: the backstop for a protocol that is otherwise cooperative.

The agent most in need of quarantine is the one least likely to keep asking
permission, so silence is treated as misbehaviour rather than as health (ADR-0001).

One rung per pass, like every other escalation. A worker silent across two passes
ends up FROZEN, and the audit trail shows both steps rather than an inferred jump.
"""

from __future__ import annotations

from .config import Settings


class Reaper:
    def __init__(self, store, clock, settings: Settings) -> None:
        self._store = store
        self._clock = clock
        self._settings = settings

    def sweep(self) -> int:
        """Escalate every run whose heartbeat is older than the timeout.

        Returns how many runs were escalated. Records cause "heartbeat_timeout" --
        distinct from any rule, because the cause an operator reads should say the
        worker went silent rather than name a detector that never fired.
        """
        raise NotImplementedError("Reaper.sweep")
