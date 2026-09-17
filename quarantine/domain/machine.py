"""The transition table and the only writer of state changes (spec §4).

Nothing outside this module assigns run.state. The gate, the rules and the judge
produce verdicts; this decides and records.

escalate() and release() are deliberately separate functions with separate
authorisation. Unifying them into a signed-delta helper is the refactor that quietly
lets the judge un-freeze a run it froze.
"""

from __future__ import annotations

from .states import RunState

# Permitted transitions. Anything absent is rejected (spec §4).
ESCALATIONS: dict[RunState, RunState] = {
    RunState.HEALTHY: RunState.DEGRADED,
    RunState.DEGRADED: RunState.FROZEN,
}

RELEASE_TARGETS: dict[RunState, frozenset[RunState]] = {
    RunState.FROZEN: frozenset({RunState.DEGRADED, RunState.HEALTHY}),
}

TERMINABLE_FROM: frozenset[RunState] = frozenset(
    {RunState.HEALTHY, RunState.DEGRADED, RunState.FROZEN}
)


def next_rung(state: RunState) -> RunState:
    """The state one rung above `state`.

    Raises IllegalTransition at the top of the ladder rather than saturating -- a
    silent no-op here would look like a successful escalation.
    """
    raise NotImplementedError("machine.next_rung")


def escalate(run, cause: str, actor: str, now, detail: str = ""):
    """Move a run exactly one rung up and record the transition.

    Returns (updated_run, transition). Never skips a rung: an immediate freeze from
    HEALTHY is two calls and two recorded transitions.
    """
    raise NotImplementedError("machine.escalate")


def auto_recover(run, now):
    """DEGRADED -> HEALTHY, actor "system". Valid only from DEGRADED.

    FROZEN never auto-recovers: an automatic system permitted to clear its own alarms
    will clear them (ADR-0002).
    """
    raise NotImplementedError("machine.auto_recover")


def release(run, target: RunState, actor: str, now, detail: str = ""):
    """Human release from FROZEN. Defaults to DEGRADED at the API layer.

    Raises AuthorizationRequired unless `actor` identifies a human.
    """
    raise NotImplementedError("machine.release")


def complete(run, now):
    """Worker finished normally -> TERMINATED, actor "system"."""
    raise NotImplementedError("machine.complete")


def terminate(run, actor: str, now, detail: str = ""):
    """Human termination -> TERMINATED. Terminal: no outgoing edges exist."""
    raise NotImplementedError("machine.terminate")
