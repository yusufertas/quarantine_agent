"""The gate decision matrix (spec §5).

The heart of the system. Every cell of state x tool_risk is enumerated here,
including the awkward ones, because an undefined cell in production is an agent
doing something nobody decided it could do.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import FakeJudge, T0, UnavailableJudge, call, make_event, make_run
from quarantine.config import Settings
from quarantine.domain.models import Verdict
from quarantine.domain.states import Decision, EventKind, RunState, Severity
from quarantine.errors import RunTerminated, UnknownRun
from quarantine.gate import Gate

LOW = "search"
HIGH = "issue_payment"
UNCLASSIFIED = "tool_nobody_registered"


@pytest.fixture
def gate_for(store, clock, settings):
    def build(judge=None):
        return Gate(store=store, judge=judge or FakeJudge(), clock=clock, settings=settings)

    return build


class TestRejections:
    def test_unknown_run_raises(self, gate_for):
        """Never a permissive default -- that would hide a bug in the caller."""
        with pytest.raises(UnknownRun):
            gate_for().decide("no-such-run", call(LOW))

    def test_terminated_run_raises_rather_than_freezing(self, store, gate_for):
        """FREEZE would look like containment working. It is a caller bug."""
        store.create_run(make_run(state=RunState.TERMINATED))
        with pytest.raises(RunTerminated):
            gate_for().decide("run-1", call(LOW))


class TestDecisionMatrix:
    @pytest.mark.parametrize(
        ("state", "tool", "expected"),
        [
            (RunState.HEALTHY, LOW, Decision.ALLOW),
            (RunState.HEALTHY, HIGH, Decision.ALLOW),
            (RunState.DEGRADED, LOW, Decision.ALLOW),
            (RunState.DEGRADED, HIGH, Decision.DENY),
            (RunState.FROZEN, LOW, Decision.FREEZE),
            (RunState.FROZEN, HIGH, Decision.FREEZE),
        ],
    )
    def test_every_cell(self, store, gate_for, state, tool, expected):
        store.create_run(make_run(state=state))
        assert gate_for().decide("run-1", call(tool)) is expected

    def test_frozen_freezes_even_a_harmless_tool(self, store, gate_for):
        """A frozen run is stopped, not merely declawed."""
        store.create_run(make_run(state=RunState.FROZEN))
        assert gate_for().decide("run-1", call(LOW)) is Decision.FREEZE

    def test_unclassified_tool_takes_the_high_risk_column(self, store, gate_for):
        store.create_run(make_run(state=RunState.DEGRADED))
        assert gate_for().decide("run-1", call(UNCLASSIFIED)) is Decision.DENY


class TestJudgePlacement:
    """Where the judge runs is decided by the tool, not the judge (ADR-0003)."""

    def test_high_risk_call_blocks_on_the_judge(self, store, gate_for):
        store.create_run(make_run(state=RunState.HEALTHY))
        judge = FakeJudge()
        gate_for(judge).decide("run-1", call(HIGH))
        assert judge.calls == ["run-1"]

    def test_low_risk_call_does_not_block_on_the_judge(self, store, gate_for):
        """The fast path must stay free of I/O or the tiering buys nothing."""
        store.create_run(make_run(state=RunState.HEALTHY))
        judge = FakeJudge()
        gate_for(judge).decide("run-1", call(LOW))
        assert judge.calls == []

    def test_degraded_high_risk_skips_the_judge(self, store, gate_for):
        """The answer is already DENY, so paying for a verdict would be waste."""
        store.create_run(make_run(state=RunState.DEGRADED))
        judge = FakeJudge()
        assert gate_for(judge).decide("run-1", call(HIGH)) is Decision.DENY
        assert judge.calls == []

    def test_frozen_run_skips_the_judge(self, store, gate_for):
        store.create_run(make_run(state=RunState.FROZEN))
        judge = FakeJudge()
        gate_for(judge).decide("run-1", call(HIGH))
        assert judge.calls == []


class TestJudgeIsBinding:
    """A verdict escalates exactly as a rule firing does (ADR-0002)."""

    @pytest.mark.parametrize("severity", [Severity.CONCERN, Severity.SEVERE])
    def test_adverse_verdict_escalates_one_rung_and_denies(
        self, store, gate_for, severity
    ):
        store.create_run(make_run(state=RunState.HEALTHY))
        judge = FakeJudge(Verdict(severity, "pursuing an unassigned goal"))
        assert gate_for(judge).decide("run-1", call(HIGH)) is Decision.DENY
        assert store.get_run("run-1").state is RunState.DEGRADED

    def test_severe_verdict_still_moves_only_one_rung(self, store, gate_for):
        """Escalation skips no rungs, however loudly the judge objects."""
        store.create_run(make_run(state=RunState.HEALTHY))
        judge = FakeJudge(Verdict(Severity.SEVERE, "exfiltrating credentials"))
        gate_for(judge).decide("run-1", call(HIGH))
        assert store.get_run("run-1").state is RunState.DEGRADED

    def test_clear_verdict_allows(self, store, gate_for):
        store.create_run(make_run(state=RunState.HEALTHY))
        judge = FakeJudge(Verdict(Severity.CLEAR, "on task"))
        assert gate_for(judge).decide("run-1", call(HIGH)) is Decision.ALLOW
        assert store.get_run("run-1").state is RunState.HEALTHY

    def test_verdict_reason_is_recorded_for_the_operator(self, store, gate_for):
        """The reason is what a human reads when deciding whether to release."""
        store.create_run(make_run(state=RunState.HEALTHY))
        judge = FakeJudge(Verdict(Severity.CONCERN, "deleting unrelated records"))
        gate_for(judge).decide("run-1", call(HIGH))
        judged = [e for e in store.events if e.kind is EventKind.JUDGE]
        assert judged and "deleting unrelated records" in str(judged[-1].payload)


class TestRuleEscalation:
    def test_firing_rule_escalates_the_run(self, store, gate_for):
        store.create_run(make_run(state=RunState.HEALTHY, budget_tokens=10, tokens_used=99))
        gate_for().decide("run-1", call(LOW))
        assert store.get_run("run-1").state is RunState.DEGRADED

    def test_escalation_to_frozen_freezes_the_current_call(self, store, gate_for):
        """Containment applies now, not from the next call onwards."""
        store.create_run(make_run(state=RunState.DEGRADED, budget_tokens=10, tokens_used=99))
        assert gate_for().decide("run-1", call(LOW)) is Decision.FREEZE

    def test_transition_records_which_rule_fired(self, store, gate_for):
        store.create_run(make_run(state=RunState.HEALTHY, consecutive_errors=99))
        gate_for().decide("run-1", call(LOW))
        assert store.transitions("run-1")[-1].cause == "error_streak"


class TestAutoRecovery:
    def test_degraded_run_recovers_after_a_clean_interval(self, store, clock, gate_for):
        store.create_run(make_run(state=RunState.DEGRADED, state_since=T0))
        clock.advance(Settings().recovery_interval + timedelta(seconds=1))
        assert gate_for().decide("run-1", call(LOW)) is Decision.ALLOW
        assert store.get_run("run-1").state is RunState.HEALTHY

    def test_no_recovery_before_the_interval_elapses(self, store, clock, gate_for):
        store.create_run(make_run(state=RunState.DEGRADED, state_since=T0))
        clock.advance(timedelta(minutes=1))
        gate_for().decide("run-1", call(LOW))
        assert store.get_run("run-1").state is RunState.DEGRADED

    def test_a_firing_rule_suppresses_recovery(self, store, clock, gate_for):
        """Otherwise a run could recover on the very pass that caught it misbehaving."""
        store.create_run(
            make_run(state=RunState.DEGRADED, state_since=T0, consecutive_errors=99)
        )
        clock.advance(Settings().recovery_interval + timedelta(seconds=1))
        gate_for().decide("run-1", call(LOW))
        assert store.get_run("run-1").state is RunState.FROZEN

    def test_frozen_runs_never_recover_automatically(self, store, clock, gate_for):
        """The property the whole design rests on (ADR-0002)."""
        store.create_run(make_run(state=RunState.FROZEN, state_since=T0))
        clock.advance(timedelta(days=30))
        assert gate_for().decide("run-1", call(LOW)) is Decision.FREEZE
        assert store.get_run("run-1").state is RunState.FROZEN


class TestEventLog:
    @pytest.mark.parametrize(
        ("state", "tool"),
        [
            (RunState.HEALTHY, LOW),
            (RunState.DEGRADED, HIGH),
            (RunState.FROZEN, LOW),
        ],
    )
    def test_every_decision_is_recorded(self, store, gate_for, state, tool):
        store.create_run(make_run(state=state))
        gate_for().decide("run-1", call(tool))
        kinds = [e.kind for e in store.events]
        assert EventKind.PROPOSED in kinds
        assert EventKind.DECISION in kinds

    def test_proposed_event_captures_the_call_the_agent_wanted_to_make(
        self, store, gate_for
    ):
        """This is what a human inspecting a frozen run actually needs (ADR-0001)."""
        store.create_run(make_run(state=RunState.FROZEN))
        gate_for().decide("run-1", call(HIGH, args_digest="payment-to-unknown-account"))
        proposed = [e for e in store.events if e.kind is EventKind.PROPOSED]
        assert proposed[-1].tool_name == HIGH
        assert proposed[-1].args_digest == "payment-to-unknown-account"

    def test_sequence_numbers_are_monotonic(self, store, gate_for):
        store.create_run(make_run())
        gate = gate_for()
        for _ in range(3):
            gate.decide("run-1", call(LOW))
        seqs = [e.seq for e in store.events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


class TestLoopDetectionUsesTheProposedCall:
    def test_the_call_about_to_repeat_counts_toward_the_loop(self, store, gate_for):
        """The gate sees proposals, so the third identical request is caught before
        it runs rather than after."""
        store.create_run(make_run(state=RunState.HEALTHY))
        for seq in range(2):
            store.append_event(
                make_event(seq=seq + 1, tool_name=LOW, args_digest="same", created_at=T0)
            )
        gate_for().decide("run-1", call(LOW, args_digest="same"))
        assert store.get_run("run-1").state is RunState.DEGRADED
