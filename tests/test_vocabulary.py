"""The vocabulary's promises: the ladder is ordered, severities are ordered,
and an actor is human by prefix.

`states.py` says `RunState` and `Severity` are "Ordered" and implements the
comparison operators; the gate escalates on `severity >= CONCERN`. Only that one
operator had ever run. The rest -- and the property that the escalation table
agrees with the rung numbers -- are cheap to pin and expensive to get wrong,
because the order IS the ladder (ADR-0002).
"""

from __future__ import annotations

import itertools

import pytest

from conftest import make_transition
from quarantine.domain import machine
from quarantine.domain.states import RunState, Severity

LADDER = [RunState.HEALTHY, RunState.DEGRADED, RunState.FROZEN, RunState.TERMINATED]
SEVERITIES = [Severity.CLEAR, Severity.CONCERN, Severity.SEVERE]


class TestTheLadderIsOrdered:
    def test_sorting_the_states_yields_the_ladder(self):
        assert sorted(RunState, reverse=True) == LADDER[::-1]
        assert sorted(RunState) == LADDER

    def test_rungs_are_consecutive_from_zero(self):
        assert [s.rung for s in LADDER] == [0, 1, 2, 3]

    @pytest.mark.parametrize(("lower", "higher"), list(itertools.combinations(LADDER, 2)))
    def test_every_pair_compares_both_ways(self, lower, higher):
        assert lower < higher and lower <= higher
        assert higher > lower and higher >= lower
        assert not (higher < lower) and not (lower > higher)

    @pytest.mark.parametrize("state", LADDER)
    def test_a_state_is_not_below_itself(self, state):
        assert not (state < state)
        assert state <= state and state >= state

    def test_comparison_with_a_stranger_is_a_type_error(self):
        """Not `False`: a string that happens to be "frozen" must not sort."""
        with pytest.raises(TypeError):
            RunState.HEALTHY < "frozen"  # noqa: B015 -- the comparison is the test
        with pytest.raises(TypeError):
            RunState.HEALTHY <= "frozen"  # noqa: B015
        with pytest.raises(TypeError):
            RunState.HEALTHY >= 1  # noqa: B015


class TestEscalationAgreesWithTheOrder:
    """`machine.ESCALATIONS` is the table; the rungs are the numbers. If they
    ever disagree, 'one rung per escalation' is true in one place and false in
    the other."""

    @pytest.mark.parametrize("source", list(machine.ESCALATIONS))
    def test_each_escalation_is_exactly_one_rung_up(self, source):
        assert machine.ESCALATIONS[source].rung == source.rung + 1

    def test_only_the_top_two_rungs_have_no_escalation(self):
        assert set(RunState) - set(machine.ESCALATIONS) == {
            RunState.FROZEN,
            RunState.TERMINATED,
        }

    def test_every_release_target_is_below_frozen(self):
        for source, targets in machine.RELEASE_TARGETS.items():
            assert all(target < source for target in targets)

    def test_terminated_is_the_top_and_nothing_leaves_it(self):
        assert max(RunState) is RunState.TERMINATED
        assert RunState.TERMINATED not in machine.ESCALATIONS
        assert RunState.TERMINATED not in machine.RELEASE_TARGETS
        assert RunState.TERMINATED not in machine.TERMINABLE_FROM


class TestSeverityIsOrdered:
    def test_sorting_yields_clear_concern_severe(self):
        assert sorted(Severity) == SEVERITIES

    @pytest.mark.parametrize(("lower", "higher"), list(itertools.combinations(SEVERITIES, 2)))
    def test_every_pair_compares_both_ways(self, lower, higher):
        assert lower < higher and higher > lower
        assert higher >= lower and lower <= higher

    def test_concern_is_the_threshold_the_gate_escalates_on(self):
        """The one comparison the gate actually performs, stated as the spec
        states it: CONCERN and above escalate, CLEAR does not."""
        escalates = [s for s in Severity if s >= Severity.CONCERN]
        assert escalates == [Severity.CONCERN, Severity.SEVERE]

    def test_comparison_with_a_stranger_is_a_type_error(self):
        with pytest.raises(TypeError):
            Severity.CLEAR < "severe"  # noqa: B015
        with pytest.raises(TypeError):
            Severity.CLEAR >= "severe"  # noqa: B015


class TestHumanActors:
    """`Transition.is_human` is what the operator surface and the machine's
    authorisation both mean by 'a human did this'."""

    @pytest.mark.parametrize(
        ("actor", "human"),
        [
            ("human:operator-1", True),
            ("system", False),
            ("judge", False),
            ("humanoid:x", False),
            ("HUMAN:operator-1", False),
            (" human:operator-1", False),
        ],
    )
    def test_prefix_is_the_whole_test(self, actor, human):
        assert make_transition(actor=actor).is_human is human
