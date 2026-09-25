"""Detector thresholds at the boundary, and the shapes the unit suite skipped.

`test_rules.py` establishes that each detector fires above its threshold and is
silent well below it. What it leaves open is the boundary itself -- `>` versus
`>=` is a one-character difference that moves the first escalation by one
call -- and two edge shapes: `loop` with nothing to count, and the current
proposal's role in `call_rate` (it does not count, because the gate snapshots
history before recording it; `loop` counts it, because it is passed in `ctx.call`).
The spec's table is the reference for every value here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import T0, call, make_event, make_run, make_transition
from quarantine.config import Settings
from quarantine.domain import rules
from quarantine.domain.models import RuleContext
from quarantine.domain.states import EventKind, RunState

SETTINGS = Settings()


def context(run=None, proposed=None, events=(), transitions=(), now=T0) -> RuleContext:
    return RuleContext(
        run=run if run is not None else make_run(),
        call=proposed,
        recent_events=tuple(events),
        recent_transitions=tuple(transitions),
        now=now,
    )


class TestCallRateBoundary:
    """Spec §7: fires when tool calls in the trailing minute are `> N`."""

    @pytest.mark.parametrize(("calls", "fires"), [(4, False), (5, False), (6, True)])
    def test_strictly_more_than_the_threshold(self, calls, fires):
        settings = Settings(call_rate_per_minute=5)
        events = [make_event(seq=i, created_at=T0) for i in range(calls)]
        assert (rules.call_rate(context(events=events), settings) is not None) is fires

    def test_a_call_exactly_one_minute_old_is_still_inside_the_window(self):
        settings = Settings(call_rate_per_minute=5)
        events = [make_event(seq=i, created_at=T0 - timedelta(minutes=1)) for i in range(6)]
        assert rules.call_rate(context(events=events, now=T0), settings) is not None

    def test_a_call_one_second_older_has_left_it(self):
        settings = Settings(call_rate_per_minute=5)
        events = [
            make_event(seq=i, created_at=T0 - timedelta(minutes=1, seconds=1))
            for i in range(6)
        ]
        assert rules.call_rate(context(events=events, now=T0), settings) is None

    def test_only_proposals_are_calls(self):
        """DECISION, OUTCOME and JUDGE rows share the log. Counting them would
        triple the apparent rate and fire on a third of the threshold."""
        settings = Settings(call_rate_per_minute=5)
        events = [
            make_event(seq=i, kind=kind, created_at=T0)
            for i, kind in enumerate(
                [EventKind.PROPOSED, EventKind.DECISION, EventKind.OUTCOME, EventKind.JUDGE] * 3
            )
        ]
        assert rules.call_rate(context(events=events), settings) is None

    def test_the_current_proposal_is_not_counted(self):
        """The gate records the proposal after snapshotting history, so the
        proposed call is not in `recent_events`; `call_rate` does not add it
        from `ctx.call` either. Pinned so a future 'fix' that counts it on both
        sides does not shift the threshold by one."""
        settings = Settings(call_rate_per_minute=5)
        events = [make_event(seq=i, created_at=T0) for i in range(5)]
        assert rules.call_rate(context(proposed=call(), events=events), settings) is None


class TestLoopBoundary:
    """Spec §7: `>= N` identical `(tool, args)` in the last M proposals."""

    @pytest.mark.parametrize(("repeats", "fires"), [(2, False), (3, True), (4, True)])
    def test_at_least_the_threshold(self, repeats, fires):
        events = [make_event(seq=i, args_digest="same") for i in range(repeats)]
        assert (rules.loop(context(events=events), SETTINGS) is not None) is fires

    def test_the_proposal_completes_a_loop_that_history_alone_does_not(self):
        """Two in history plus the one about to happen: caught before it runs."""
        events = [make_event(seq=i, args_digest="same") for i in range(2)]
        assert rules.loop(context(events=events), SETTINGS) is None
        assert rules.loop(context(proposed=call(args_digest="same"), events=events), SETTINGS)

    def test_nothing_to_count_is_silent(self):
        assert rules.loop(context(), SETTINGS) is None

    def test_a_single_proposal_is_not_a_loop(self):
        assert rules.loop(context(proposed=call()), Settings(loop_repeats=1)) is not None
        assert rules.loop(context(proposed=call()), Settings(loop_repeats=2)) is None

    def test_same_tool_different_arguments_is_not_a_loop(self):
        events = [make_event(seq=i, args_digest=f"d{i}") for i in range(10)]
        assert rules.loop(context(proposed=call(args_digest="d99"), events=events), SETTINGS) is None

    def test_same_arguments_different_tool_is_not_a_loop(self):
        events = [make_event(seq=i, tool_name=f"tool{i}", args_digest="same") for i in range(10)]
        assert rules.loop(context(events=events), SETTINGS) is None

    def test_the_window_is_measured_in_proposals_not_rows(self):
        """The reading guide's trap: three identical proposals separated by
        enough non-proposal rows to overflow a row-sliced window must still
        count as three."""
        settings = Settings(loop_repeats=3, loop_window=3)
        events = []
        for i in range(3):
            events.append(make_event(seq=len(events), args_digest="same"))
            events += [
                make_event(seq=len(events) + j, kind=EventKind.DECISION, decision="allow")
                for j in range(3)
            ]
        assert rules.loop(context(events=events), settings) is not None

    def test_the_detail_names_the_tool_and_the_count(self):
        events = [make_event(seq=i, tool_name="read_file", args_digest="same") for i in range(3)]
        verdict = rules.loop(context(events=events), SETTINGS)
        assert verdict.rule == "loop"
        assert "read_file" in verdict.detail and "3x" in verdict.detail


class TestWallClockBoundary:
    """Spec §7: `now > deadline_at` -- the deadline itself is still allowed."""

    def test_exactly_at_the_deadline_is_silent(self):
        run = make_run(deadline_after=timedelta(minutes=30))
        assert rules.wall_clock(context(run, now=T0 + timedelta(minutes=30)), SETTINGS) is None

    def test_one_microsecond_past_it_fires(self):
        run = make_run(deadline_after=timedelta(minutes=30))
        ctx = context(run, now=T0 + timedelta(minutes=30, microseconds=1))
        verdict = rules.wall_clock(ctx, SETTINGS)
        assert verdict is not None
        assert run.deadline_at.isoformat() in verdict.detail


class TestFlapBoundary:
    """Spec §7: `>= N` HEALTHY->DEGRADED transitions inside the window."""

    def degradations(self, count: int, *, age: timedelta = timedelta(0)):
        return [
            make_transition(
                from_state=RunState.HEALTHY,
                to_state=RunState.DEGRADED,
                created_at=T0 - age,
            )
            for _ in range(count)
        ]

    @pytest.mark.parametrize(("count", "fires"), [(2, False), (3, True)])
    def test_at_least_the_threshold(self, count, fires):
        ctx = context(transitions=self.degradations(count), now=T0)
        assert (rules.flap(ctx, SETTINGS) is not None) is fires

    def test_a_degradation_exactly_at_the_window_edge_still_counts(self):
        ctx = context(transitions=self.degradations(3, age=SETTINGS.flap_window), now=T0)
        assert rules.flap(ctx, SETTINGS) is not None

    def test_one_second_older_does_not(self):
        age = SETTINGS.flap_window + timedelta(seconds=1)
        ctx = context(transitions=self.degradations(3, age=age), now=T0)
        assert rules.flap(ctx, SETTINGS) is None

    def test_heartbeat_and_judge_degradations_count_alike(self):
        """It is the edge that matters, not who caused it: three degradations
        by three different actors within the hour is still flapping."""
        transitions = [
            make_transition(from_state=RunState.HEALTHY, to_state=RunState.DEGRADED, cause=cause)
            for cause in ("loop", "judge", "heartbeat_timeout")
        ]
        assert rules.flap(context(transitions=transitions), SETTINGS) is not None


class TestBudgetBoundary:
    def test_spend_is_checked_independently_of_tokens(self):
        run = make_run(budget_tokens=100, tokens_used=0, budget_cost_cents=10, cost_cents=11)
        verdict = rules.budget(context(run), SETTINGS)
        assert verdict is not None and "c" in verdict.detail

    def test_exactly_at_the_spend_budget_is_silent(self):
        run = make_run(budget_cost_cents=10, cost_cents=10)
        assert rules.budget(context(run), SETTINGS) is None

    def test_tokens_are_reported_before_spend_when_both_are_over(self):
        run = make_run(budget_tokens=1, tokens_used=2, budget_cost_cents=1, cost_cents=2)
        assert "tokens" in rules.budget(context(run), SETTINGS).detail


class TestRuleOrderIsTheRecordedCause:
    """Spec §7: the first rule in RULES order wins, so the cause an operator
    reads is deterministic when several fire on one pass."""

    def test_budget_outranks_a_loop(self):
        run = make_run(budget_tokens=1, tokens_used=2)
        events = [make_event(seq=i, args_digest="same") for i in range(3)]
        assert rules.first_firing(context(run, events=events), SETTINGS).rule == "budget"

    def test_wall_clock_outranks_call_rate_and_flap(self):
        run = make_run(deadline_after=timedelta(0))
        events = [make_event(seq=i, args_digest=f"d{i}", created_at=T0) for i in range(100)]
        transitions = [
            make_transition(from_state=RunState.HEALTHY, to_state=RunState.DEGRADED)
            for _ in range(5)
        ]
        ctx = context(run, events=events, transitions=transitions, now=T0 + timedelta(seconds=1))
        assert rules.first_firing(ctx, SETTINGS).rule == "wall_clock"

    def test_the_documented_order(self):
        assert [rule.__name__ for rule in rules.RULES] == [
            "budget",
            "loop",
            "error_streak",
            "wall_clock",
            "call_rate",
            "flap",
        ]
