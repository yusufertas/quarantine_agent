"""Unreachable dependencies (spec §9).

The tiering is the whole answer: HIGH-risk calls fail closed, LOW-risk calls fail
open. An outage therefore degrades every agent to safe mode rather than halting them
or leaving them unguarded.

These are the tests that are nearly impossible to write without injection, and the
reason the judge sits behind a protocol.
"""

from __future__ import annotations

import pytest

from conftest import FakeJudge, UnavailableJudge, call, make_run
from quarantine.domain.models import Verdict
from quarantine.domain.states import Decision, EventKind, RunState, Severity
from quarantine.gate import Gate

LOW = "search"
HIGH = "issue_payment"


@pytest.fixture
def gate_for(store, clock, settings):
    def build(judge):
        return Gate(store=store, judge=judge, clock=clock, settings=settings)

    return build


class TestJudgeUnavailable:
    def test_high_risk_call_fails_closed(self, store, gate_for):
        """No supervision, no destructive action."""
        store.create_run(make_run(state=RunState.HEALTHY))
        assert gate_for(UnavailableJudge()).decide("run-1", call(HIGH)) is Decision.DENY

    def test_low_risk_call_fails_open(self, store, gate_for):
        """A read-only call cannot cause the harm the judge is there to prevent."""
        store.create_run(make_run(state=RunState.HEALTHY))
        assert gate_for(UnavailableJudge()).decide("run-1", call(LOW)) is Decision.ALLOW

    def test_failure_is_never_swallowed(self, store, gate_for):
        """A silently-caught timeout is a system that appears to be judging and isn't."""
        store.create_run(make_run(state=RunState.HEALTHY))
        gate_for(UnavailableJudge()).decide("run-1", call(HIGH))
        judged = [e for e in store.events if e.kind is EventKind.JUDGE]
        assert judged, "judge failure must still append a JUDGE event"

    def test_unavailable_judge_does_not_escalate_the_run(self, store, gate_for):
        """Our outage is not the agent's misbehaviour. Denying the call is enough."""
        store.create_run(make_run(state=RunState.HEALTHY))
        gate_for(UnavailableJudge()).decide("run-1", call(HIGH))
        assert store.get_run("run-1").state is RunState.HEALTHY


class TestEscalatedWorkerResumes:
    def test_resuming_after_escalation_does_not_restore_the_run(self, store, gate_for):
        """Being alive again is not evidence of being well (spec §9)."""
        store.create_run(make_run(state=RunState.FROZEN))
        gate = gate_for(FakeJudge())
        for _ in range(5):
            assert gate.decide("run-1", call(LOW)) is Decision.FREEZE
        assert store.get_run("run-1").state is RunState.FROZEN

    def test_a_degraded_worker_that_keeps_working_stays_declawed(self, store, gate_for):
        store.create_run(make_run(state=RunState.DEGRADED))
        gate = gate_for(FakeJudge())
        assert gate.decide("run-1", call(LOW)) is Decision.ALLOW
        assert gate.decide("run-1", call(HIGH)) is Decision.DENY


class TestStoreUnavailable:
    def test_gate_propagates_storage_failure(self, clock, settings):
        """The control plane cannot decide without state, so it must not pretend to."""

        class BrokenStore:
            def get_run(self, run_id):
                raise RuntimeError("database is gone")

        gate = Gate(store=BrokenStore(), judge=FakeJudge(), clock=clock, settings=settings)
        with pytest.raises(RuntimeError, match="database is gone"):
            gate.decide("run-1", call(LOW))
