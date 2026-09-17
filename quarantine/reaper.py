"""Heartbeat reaper: the backstop for a protocol that is otherwise cooperative.

The agent most in need of quarantine is the one least likely to keep asking
permission, so silence is treated as misbehaviour rather than as health (ADR-0001).

One rung per pass, like every other escalation. A worker silent across two passes
ends up FROZEN, and the audit trail shows both steps rather than an inferred jump.
"""

from __future__ import annotations

from .config import Settings
from .domain import machine


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
        now = self._clock.now()
        cutoff = now - self._settings.heartbeat_timeout
        escalated = 0

        for run in self._store.runs_with_stale_heartbeat(cutoff):
            # A FROZEN run is supposed to be silent -- escalating it further is
            # a human decision, not a consequence of doing what it was told.
            if run.state not in machine.ESCALATIONS:
                continue

            # One escalation per timeout window. Without this the reaper would
            # re-escalate the same run on every pass, walking it to FROZEN in
            # as many sweeps as happen to fit before the worker could recover.
            if now - run.state_since < self._settings.heartbeat_timeout:
                continue

            updated, transition = machine.escalate(
                run, cause="heartbeat_timeout", actor="system", now=now
            )
            self._store.save_run(updated)
            self._store.append_transition(transition)
            escalated += 1

        return escalated
