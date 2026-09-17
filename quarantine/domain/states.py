"""The vocabulary of the system: the quarantine ladder and the gate's answers."""

from __future__ import annotations

from enum import Enum


class RunState(Enum):
    """Where a run sits on the quarantine ladder (ADR-0002).

    Ordered. A run occupies exactly one state, and escalation moves exactly one
    rung -- an immediate freeze is two recorded transitions, never one.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FROZEN = "frozen"
    TERMINATED = "terminated"

    @property
    def rung(self) -> int:
        return _RUNGS[self]

    def __lt__(self, other: RunState) -> bool:
        if not isinstance(other, RunState):
            return NotImplemented
        return self.rung < other.rung

    def __le__(self, other: RunState) -> bool:
        if not isinstance(other, RunState):
            return NotImplemented
        return self.rung <= other.rung


_RUNGS = {
    RunState.HEALTHY: 0,
    RunState.DEGRADED: 1,
    RunState.FROZEN: 2,
    RunState.TERMINATED: 3,
}


class Decision(Enum):
    """The gate's answer to a proposed tool call."""

    ALLOW = "allow"
    DENY = "deny"
    FREEZE = "freeze"


class ToolRisk(Enum):
    """Blast radius of a tool (ADR-0003).

    Drives four separate behaviours: whether the judge runs synchronously, whether
    the call is permitted in DEGRADED, and whether an unreachable dependency fails
    open or closed.
    """

    LOW = "low"
    HIGH = "high"


class Severity(Enum):
    """A judge verdict's seriousness. Ordered; CONCERN and above escalate."""

    CLEAR = "clear"
    CONCERN = "concern"
    SEVERE = "severe"

    @property
    def level(self) -> int:
        return _SEVERITIES[self]

    def __lt__(self, other: Severity) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.level < other.level

    def __ge__(self, other: Severity) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.level >= other.level


_SEVERITIES = {Severity.CLEAR: 0, Severity.CONCERN: 1, Severity.SEVERE: 2}


class EventKind(Enum):
    """Kinds of row in the append-only event log."""

    PROPOSED = "proposed"
    DECISION = "decision"
    OUTCOME = "outcome"
    JUDGE = "judge"
    SYSTEM = "system"
