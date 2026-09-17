"""The transition table (spec §4).

A transition table is one of the very few things you can test exhaustively, so this
file does: every (from, to) pair is either explicitly permitted or explicitly
rejected. The forbidden pairs matter more than the permitted ones -- they are where a
future refactor silently lets the judge un-freeze a run it froze.
"""

from __future__ import annotations

import itertools

import pytest

from conftest import T0, make_run
from quarantine.domain import machine
from quarantine.domain.states import RunState
from quarantine.errors import AuthorizationRequired, IllegalTransition

HUMAN = "human:operator-1"


class TestEscalation:
    def test_healthy_escalates_to_degraded(self):
        run, transition = machine.escalate(
            make_run(state=RunState.HEALTHY), cause="loop", actor="system", now=T0
        )
        assert run.state is RunState.DEGRADED
        assert transition.from_state is RunState.HEALTHY
        assert transition.to_state is RunState.DEGRADED

    def test_degraded_escalates_to_frozen(self):
        run, _ = machine.escalate(
            make_run(state=RunState.DEGRADED), cause="loop", actor="system", now=T0
        )
        assert run.state is RunState.FROZEN

    def test_escalation_never_skips_a_rung(self):
        """An immediate freeze is two transitions, never one.

        'What happened to this run' must never require inferring a skipped step.
        """
        run, first = machine.escalate(
            make_run(state=RunState.HEALTHY), cause="judge", actor="system", now=T0
        )
        assert run.state is RunState.DEGRADED
        run, second = machine.escalate(run, cause="judge", actor="system", now=T0)
        assert run.state is RunState.FROZEN
        assert (first.to_state, second.to_state) == (RunState.DEGRADED, RunState.FROZEN)

    def test_escalation_records_cause_and_actor(self):
        _, transition = machine.escalate(
            make_run(), cause="error_streak", actor="system", now=T0, detail="5 in a row"
        )
        assert transition.cause == "error_streak"
        assert transition.actor == "system"
        assert transition.detail == "5 in a row"

    def test_escalation_stamps_state_since(self):
        """Auto-recovery measures time in DEGRADED, so entering it must set the clock."""
        run, _ = machine.escalate(make_run(), cause="loop", actor="system", now=T0)
        assert run.state_since == T0

    @pytest.mark.parametrize("state", [RunState.FROZEN, RunState.TERMINATED])
    def test_escalation_at_top_of_ladder_raises(self, state):
        """Saturating silently would read as a successful escalation."""
        with pytest.raises(IllegalTransition):
            machine.escalate(make_run(state=state), cause="loop", actor="system", now=T0)


class TestAutoRecovery:
    def test_degraded_recovers_to_healthy(self):
        run, transition = machine.auto_recover(make_run(state=RunState.DEGRADED), now=T0)
        assert run.state is RunState.HEALTHY
        assert transition.actor == "system"

    @pytest.mark.parametrize(
        "state", [RunState.HEALTHY, RunState.FROZEN, RunState.TERMINATED]
    )
    def test_auto_recovery_applies_only_to_degraded(self, state):
        """FROZEN especially: nothing automatic may ever clear a freeze (ADR-0002)."""
        with pytest.raises(IllegalTransition):
            machine.auto_recover(make_run(state=state), now=T0)


class TestRelease:
    @pytest.mark.parametrize("target", [RunState.DEGRADED, RunState.HEALTHY])
    def test_human_releases_frozen_run_to_either_lower_state(self, target):
        run, transition = machine.release(
            make_run(state=RunState.FROZEN), target=target, actor=HUMAN, now=T0
        )
        assert run.state is target
        assert transition.actor == HUMAN
        assert transition.is_human

    def test_release_requires_a_human_actor(self):
        """The whole point of the state. A system actor must be refused."""
        with pytest.raises(AuthorizationRequired):
            machine.release(
                make_run(state=RunState.FROZEN),
                target=RunState.DEGRADED,
                actor="system",
                now=T0,
            )

    def test_release_cannot_raise_a_run_up_the_ladder(self):
        with pytest.raises(IllegalTransition):
            machine.release(
                make_run(state=RunState.FROZEN),
                target=RunState.TERMINATED,
                actor=HUMAN,
                now=T0,
            )

    @pytest.mark.parametrize("state", [RunState.HEALTHY, RunState.DEGRADED])
    def test_release_only_applies_to_frozen_runs(self, state):
        with pytest.raises(IllegalTransition):
            machine.release(
                make_run(state=state), target=RunState.HEALTHY, actor=HUMAN, now=T0
            )


class TestTermination:
    @pytest.mark.parametrize("state", [RunState.HEALTHY, RunState.DEGRADED])
    def test_worker_completes_normally(self, state):
        run, transition = machine.complete(make_run(state=state), now=T0)
        assert run.state is RunState.TERMINATED
        assert transition.actor == "system"

    def test_completing_a_frozen_run_is_rejected(self):
        """A frozen run ends by human decision, not by its worker declaring success."""
        with pytest.raises(IllegalTransition):
            machine.complete(make_run(state=RunState.FROZEN), now=T0)

    @pytest.mark.parametrize(
        "state", [RunState.HEALTHY, RunState.DEGRADED, RunState.FROZEN]
    )
    def test_human_can_terminate_from_any_live_state(self, state):
        run, _ = machine.terminate(make_run(state=state), actor=HUMAN, now=T0)
        assert run.state is RunState.TERMINATED

    def test_termination_requires_a_human_actor(self):
        with pytest.raises(AuthorizationRequired):
            machine.terminate(make_run(), actor="system", now=T0)


class TestTerminalIsTerminal:
    """Invariant 1: TERMINATED has no outgoing edges."""

    @pytest.mark.parametrize(
        "operation",
        [
            lambda r: machine.escalate(r, cause="loop", actor="system", now=T0),
            lambda r: machine.auto_recover(r, now=T0),
            lambda r: machine.release(r, target=RunState.HEALTHY, actor=HUMAN, now=T0),
            lambda r: machine.complete(r, now=T0),
            lambda r: machine.terminate(r, actor=HUMAN, now=T0),
        ],
    )
    def test_no_operation_moves_a_terminated_run(self, operation):
        with pytest.raises(IllegalTransition):
            operation(make_run(state=RunState.TERMINATED))


class TestExhaustiveCoverage:
    """Every (from, to) pair is accounted for by some permitted operation, or by none.

    This is the test that catches a transition quietly added to the table without a
    decision behind it.
    """

    PERMITTED = {
        (RunState.HEALTHY, RunState.DEGRADED),
        (RunState.DEGRADED, RunState.FROZEN),
        (RunState.DEGRADED, RunState.HEALTHY),
        (RunState.FROZEN, RunState.DEGRADED),
        (RunState.FROZEN, RunState.HEALTHY),
        (RunState.HEALTHY, RunState.TERMINATED),
        (RunState.DEGRADED, RunState.TERMINATED),
        (RunState.FROZEN, RunState.TERMINATED),
    }

    @pytest.mark.parametrize(
        ("source", "target"), list(itertools.product(RunState, RunState))
    )
    def test_pair_is_reachable_exactly_when_permitted(self, source, target):
        reached = False
        for operation in (
            lambda r: machine.escalate(r, cause="x", actor="system", now=T0),
            lambda r: machine.auto_recover(r, now=T0),
            lambda r: machine.release(r, target=target, actor=HUMAN, now=T0),
            lambda r: machine.complete(r, now=T0),
            lambda r: machine.terminate(r, actor=HUMAN, now=T0),
        ):
            try:
                run, _ = operation(make_run(state=source))
            except (IllegalTransition, AuthorizationRequired):
                continue
            if run.state is target:
                reached = True
                break

        assert reached is ((source, target) in self.PERMITTED), (
            f"{source.value} -> {target.value} reachable={reached}"
        )
