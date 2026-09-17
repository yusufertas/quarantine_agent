"""Immutable value objects. These are definitions, not behaviour."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .states import EventKind, RunState, Severity


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool call a worker proposes to make, seen before it runs.

    The gate sees the proposal rather than the result, which is what makes a frozen
    run hold the decision it was about to take (ADR-0001).
    """

    tool_name: str
    args_digest: str
    args_preview: str = ""


@dataclass(frozen=True, slots=True)
class Run:
    """A single agent run and the counters the deterministic rules read."""

    id: str
    agent_name: str
    state: RunState
    state_since: datetime
    created_at: datetime
    last_heartbeat_at: datetime
    budget_tokens: int
    budget_cost_cents: int
    deadline_at: datetime
    tokens_used: int = 0
    cost_cents: int = 0
    tool_calls: int = 0
    consecutive_errors: int = 0


@dataclass(frozen=True, slots=True)
class Event:
    """One append-only row of a run's trajectory.

    Serves three readers at once: the judge, the operator inspecting a frozen run,
    and the audit trail. Nothing edits or deletes these.
    """

    run_id: str
    seq: int
    kind: EventKind
    created_at: datetime
    tool_name: str | None = None
    args_digest: str | None = None
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Transition:
    """A recorded state change. Written only by domain.machine."""

    run_id: str
    from_state: RunState
    to_state: RunState
    cause: str
    actor: str
    created_at: datetime
    detail: str = ""

    @property
    def is_human(self) -> bool:
        return self.actor.startswith("human:")


@dataclass(frozen=True, slots=True)
class Verdict:
    """The judge's opinion. The reason is what an operator reads (spec §8)."""

    severity: Severity
    reason: str


@dataclass(frozen=True, slots=True)
class RuleVerdict:
    """A fired rule. The name becomes the transition's recorded cause."""

    rule: str
    detail: str


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Everything a rule may read, assembled by the caller.

    Rules receive this rather than reaching for storage, which is how they stay pure
    functions that table-driven tests can exercise without fixtures (spec §11).
    """

    run: Run
    call: ToolCall | None
    recent_events: tuple[Event, ...]
    recent_transitions: tuple[Transition, ...]
    now: datetime
