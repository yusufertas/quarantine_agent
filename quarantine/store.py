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

    def save_run(self, run: Run) -> None:
        """Persist the run's counters and heartbeat. It CANNOT write `state`.

        Callers hold a snapshot read moments earlier, so writing the whole row
        back would erase any escalation that landed in between -- while the
        `transitions` row survives, leaving a run whose audit trail says
        DEGRADED and whose state says HEALTHY. Storage enforces the
        machine-only-writer invariant (spec §4) rather than trusting every
        caller to remember it; `record_state_change` is the only state path.
        """

    def record_state_change(self, run: Run, transition: Transition) -> None:
        """Write a state change and its audit row atomically.

        `run` must be the output of a `domain.machine` function and `transition`
        its companion. Only `state` and `state_since` are taken from `run` --
        counters belong to `save_run` -- so a concurrent outcome report cannot be
        clobbered by an escalation, or the reverse.

        One transaction: spec §4 invariant 4 requires that no state change exist
        without its transition row, which two separate writes cannot promise.
        """

    def list_runs(self, state: RunState | None = None) -> Sequence[Run]: ...

    def append_event(self, event: Event) -> None:
        """Append-only. There is deliberately no update or delete.

        The repository allocates `seq`: allocation and insertion must be one
        atomic step, so `event.seq` is advisory and may be replaced. Reading the
        high-water mark in one statement and inserting in another is a race that
        loses rows to the `(run_id, seq)` primary key under concurrency.
        """

    def recent_events(self, run_id: str, limit: int) -> Sequence[Event]: ...

    def next_seq(self, run_id: str) -> int:
        """The next sequence number. Advisory only -- see `append_event`."""

    def append_transition(self, transition: Transition) -> None:
        """Append an audit row on its own. A state change uses
        `record_state_change` instead, which cannot leave the two out of step."""

    def transitions(self, run_id: str) -> Sequence[Transition]: ...

    def runs_with_stale_heartbeat(self, cutoff: datetime) -> Sequence[Run]: ...
