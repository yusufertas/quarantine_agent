"""Persistence boundary.

SQLite sits behind this protocol, which is why the storage choice is recorded in the
spec rather than an ADR -- it is reversible by construction.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from .domain.models import Event, Run, Transition
from .domain.states import RunState


class Repository(Protocol):
    def create_run(self, run: Run) -> None: ...

    def get_run(self, run_id: str) -> Run:
        """Raises UnknownRun if absent. Never returns a permissive placeholder."""

    def save_run(self, run: Run) -> None: ...

    def list_runs(self, state: RunState | None = None) -> Sequence[Run]: ...

    def append_event(self, event: Event) -> None:
        """Append-only. There is deliberately no update or delete."""

    def recent_events(self, run_id: str, limit: int) -> Sequence[Event]: ...

    def next_seq(self, run_id: str) -> int: ...

    def append_transition(self, transition: Transition) -> None: ...

    def transitions(self, run_id: str) -> Sequence[Transition]: ...

    def runs_with_stale_heartbeat(self, cutoff: datetime) -> Sequence[Run]: ...
