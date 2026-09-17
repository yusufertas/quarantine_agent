"""The transition table and the only writer of state changes (spec §4).

Nothing outside this module assigns run.state. The gate, the rules and the judge
produce verdicts; this decides and records.

escalate() and release() are deliberately separate functions with separate
authorisation. Unifying them into a signed-delta helper is the refactor that quietly
lets the judge un-freeze a run it froze.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from ..errors import AuthorizationRequired, IllegalTransition
from .models import Run, Transition
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


def _require_human(actor: str) -> None:
    """Escalation is automatic; de-escalation is not.

    A system actor reaching this point means some automatic path is trying to
    clear a containment it caused, which is the failure mode ADR-0002 exists
    to prevent.
    """
    if not actor.startswith("human:"):
        raise AuthorizationRequired(
            f"actor {actor!r} is not a human; this operation requires one"
        )


def _apply(
    run: Run,
    target: RunState,
    *,
    cause: str,
    actor: str,
    now: datetime,
    detail: str = "",
) -> tuple[Run, Transition]:
    """Build the new run and its audit row. The single place state changes."""
    return (
        replace(run, state=target, state_since=now),
        Transition(
            run_id=run.id,
            from_state=run.state,
            to_state=target,
            cause=cause,
            actor=actor,
            created_at=now,
            detail=detail,
        ),
    )


def next_rung(state: RunState) -> RunState:
    """The state one rung above `state`.

    Raises IllegalTransition at the top of the ladder rather than saturating -- a
    silent no-op here would look like a successful escalation.
    """
    try:
        return ESCALATIONS[state]
    except KeyError:
        raise IllegalTransition(
            state, None, "already at or past the top of the ladder"
        ) from None


def escalate(run, cause: str, actor: str, now, detail: str = ""):
    """Move a run exactly one rung up and record the transition.

    Returns (updated_run, transition). Never skips a rung: an immediate freeze from
    HEALTHY is two calls and two recorded transitions.
    """
    return _apply(run, next_rung(run.state), cause=cause, actor=actor, now=now, detail=detail)


def auto_recover(run, now):
    """DEGRADED -> HEALTHY, actor "system". Valid only from DEGRADED.

    FROZEN never auto-recovers: an automatic system permitted to clear its own alarms
    will clear them (ADR-0002).
    """
    if run.state is not RunState.DEGRADED:
        raise IllegalTransition(
            run.state, RunState.HEALTHY, "auto-recovery applies only to DEGRADED"
        )
    return _apply(run, RunState.HEALTHY, cause="clean_interval", actor="system", now=now)


def release(run, target: RunState, actor: str, now, detail: str = ""):
    """Human release from FROZEN. Defaults to DEGRADED at the API layer.

    Raises AuthorizationRequired unless `actor` identifies a human.
    """
    _require_human(actor)
    if target not in RELEASE_TARGETS.get(run.state, frozenset()):
        raise IllegalTransition(run.state, target, "not a permitted release target")
    return _apply(run, target, cause="release", actor=actor, now=now, detail=detail)


def complete(run, now):
    """Worker finished normally -> TERMINATED, actor "system"."""
    if run.state not in (RunState.HEALTHY, RunState.DEGRADED):
        raise IllegalTransition(
            run.state,
            RunState.TERMINATED,
            "a contained run ends by human decision, not by its worker declaring success",
        )
    return _apply(run, RunState.TERMINATED, cause="completed", actor="system", now=now)


def terminate(run, actor: str, now, detail: str = ""):
    """Human termination -> TERMINATED. Terminal: no outgoing edges exist."""
    _require_human(actor)
    if run.state not in TERMINABLE_FROM:
        raise IllegalTransition(run.state, RunState.TERMINATED, "run is already terminated")
    return _apply(
        run, RunState.TERMINATED, cause="terminated", actor=actor, now=now, detail=detail
    )
