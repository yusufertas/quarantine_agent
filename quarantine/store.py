"""Persistence boundary.

SQLite sits behind this protocol, which is why the storage choice is recorded in the
spec rather than an ADR -- it is reversible by construction.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from .domain.models import Event, Run, Transition
from .domain.states import Decision, RunState

MAX_COUNTER = 2**63 - 1


class Repository(Protocol):
    def create_run(self, run: Run) -> None: ...

    def get_run(self, run_id: str) -> Run:
        """Raises UnknownRun if absent. Never returns a permissive placeholder."""

    def save_run(self, run: Run) -> None:
        """Legacy snapshot write for compatibility. It CANNOT write `state`.

        Not concurrency-safe for counters: request handlers must use
        record_outcome or touch_heartbeat, never this method.

        Callers hold a snapshot read moments earlier, so writing the whole row
        back would erase any escalation that landed in between -- while the
        `transitions` row survives, leaving a run whose audit trail says
        DEGRADED and whose state says HEALTHY. Storage enforces the
        machine-only-writer invariant (spec §4) rather than trusting every
        caller to remember it; `record_state_change` is the only state path.
        """

    def record_outcome(self, event: Event) -> None:
        """Atomically add usage deltas, update liveness, and append the outcome.

        Reject negative/overflowing usage with InvalidUsage. Never write state.
        """

    def touch_heartbeat(self, run_id: str, now: datetime) -> None:
        """Advance only liveness, never overwrite counters with a snapshot."""

    def record_state_change(
        self, run: Run, transition: Transition, *, expected_heartbeat: datetime | None = None,
    ) -> Run:
        """Write a state change and its audit row atomically.

        `run` must be the output of a `domain.machine` function and `transition`
        its companion. Only `state` and `state_since` are taken from `run` --
        counters belong to `record_outcome` -- so a concurrent outcome report cannot be
        clobbered by an escalation, or the reverse.

        One transaction: spec §4 invariant 4 requires that no state change exist
        without its transition row, which two separate writes cannot promise.

        Compare run.state_revision and transition.from_state against current
        storage before writing. Raise ConcurrentStateChange on a stale snapshot,
        including a state that cycled back to its old value; return the committed
        run with its new revision. No lock is held while a judge evaluates.
        When expected_heartbeat is supplied, require it to remain unchanged in
        the same transaction: a timeout depends on liveness as well as state.
        """

    def record_decision(self, event: Event, expected_revision: int) -> Decision:
        """Validate containment and append a decision in one transaction.

        TERMINATED raises RunTerminated; FROZEN returns FREEZE. An ALLOW based on
        a changed revision becomes DENY, never a new authorization. The returned
        decision must match the persisted event.
        """

    def list_runs(self, state: RunState | None = None) -> Sequence[Run]: ...

    def append_event(self, event: Event) -> int:
        """Append-only. There is deliberately no update or delete.

        The repository allocates `seq`: allocation and insertion must be one
        atomic step, so `event.seq` is advisory and may be replaced. Reading the
        high-water mark in one statement and inserting in another is a race that
        loses rows to the `(run_id, seq)` primary key under concurrency.
        Return the allocated sequence, not the caller's advisory value.
        """

    def count_proposals(self, run_id: str, through_seq: int, tools: Sequence[str]) -> int:
        """Count proposals for the given tools up to an allocated sequence.

        Bounding by this call's sequence keeps concurrent callers from sharing
        another call's ordinal; unrelated event kinds never affect sampling.
        """

    def recent_events(self, run_id: str, limit: int) -> Sequence[Event]: ...

    def next_seq(self, run_id: str) -> int:
        """The next sequence number. Advisory only -- see `append_event`."""

    def append_transition(self, transition: Transition) -> None:
        """Append an audit row on its own. A state change uses
        `record_state_change` instead, which cannot leave the two out of step."""

    def transitions(self, run_id: str) -> Sequence[Transition]: ...

    def runs_with_stale_heartbeat(self, cutoff: datetime) -> Sequence[Run]: ...
