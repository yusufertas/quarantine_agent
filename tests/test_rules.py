"""Deterministic detectors (spec §7).

Table-driven, no fixtures, no clock, no database -- which is the payoff for rules
being pure functions of a RuleContext. If a rule ever needs a fixture here, it has
acquired I/O and the LOW-risk gate path is no longer fast.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import T0, call, make_event, make_run, make_transition
from quarantine.config import Settings
from quarantine.domain import rules
from quarantine.domain.models import RuleContext
from quarantine.domain.states import RunState

SETTINGS = Settings()


def context(run=None, proposed=None, events=(), transitions=(), now=T0) -> RuleContext:
    return RuleContext(
        run=run if run is not None else make_run(),
        call=proposed,
        recent_events=tuple(events),
        recent_transitions=tuple(transitions),
        now=now,
    )


class TestBudget:
    def test_fires_when_tokens_exceeded(self):
        ctx = context(make_run(budget_tokens=100, tokens_used=101))
        verdict = rules.budget(ctx, SETTINGS)
        assert verdict is not None
        assert verdict.rule == "budget"

    def test_fires_when_spend_exceeded(self):
        ctx = context(make_run(budget_cost_cents=50, cost_cents=51))
        assert rules.budget(ctx, SETTINGS) is not None

    def test_silent_exactly_at_the_budget(self):
        """The budget is what you may spend, not what you may exceed by one."""
        ctx = context(make_run(budget_tokens=100, tokens_used=100))
        assert rules.budget(ctx, SETTINGS) is None


class TestLoop:
    def test_fires_on_repeated_identical_calls(self):
        events = [make_event(seq=i, tool_name="search", args_digest="same") for i in range(3)]
        assert rules.loop(context(events=events), SETTINGS) is not None

    def test_silent_below_the_repeat_threshold(self):
        events = [make_event(seq=i, tool_name="search", args_digest="same") for i in range(2)]
        assert rules.loop(context(events=events), SETTINGS) is None

    def test_different_arguments_are_not_a_loop(self):
        """An agent calling the same tool with new inputs is working, not stuck."""
        events = [make_event(seq=i, tool_name="search", args_digest=f"d{i}") for i in range(5)]
        assert rules.loop(context(events=events), SETTINGS) is None

    def test_repeats_outside_the_window_do_not_count(self):
        settings = Settings(loop_repeats=3, loop_window=4)
        events = [make_event(seq=0, tool_name="search", args_digest="same")]
        events += [make_event(seq=i, tool_name="read_file", args_digest=f"d{i}") for i in range(1, 4)]
        events += [make_event(seq=4, tool_name="search", args_digest="same")]
        assert rules.loop(context(events=events), settings) is None


class TestErrorStreak:
    @pytest.mark.parametrize(("errors", "fires"), [(4, False), (5, True), (9, True)])
    def test_threshold(self, errors, fires):
        ctx = context(make_run(consecutive_errors=errors))
        assert (rules.error_streak(ctx, SETTINGS) is not None) is fires


class TestWallClock:
    def test_fires_past_the_deadline(self):
        run = make_run(deadline_after=timedelta(minutes=30))
        ctx = context(run, now=T0 + timedelta(minutes=31))
        assert rules.wall_clock(ctx, SETTINGS) is not None

    def test_silent_before_the_deadline(self):
        run = make_run(deadline_after=timedelta(minutes=30))
        ctx = context(run, now=T0 + timedelta(minutes=29))
        assert rules.wall_clock(ctx, SETTINGS) is None


class TestCallRate:
    def test_fires_above_threshold_within_the_trailing_minute(self):
        settings = Settings(call_rate_per_minute=5)
        events = [make_event(seq=i, created_at=T0 - timedelta(seconds=i)) for i in range(6)]
        assert rules.call_rate(context(events=events, now=T0), settings) is not None

    def test_older_calls_fall_out_of_the_window(self):
        settings = Settings(call_rate_per_minute=5)
        events = [make_event(seq=i, created_at=T0 - timedelta(minutes=5)) for i in range(20)]
        assert rules.call_rate(context(events=events, now=T0), settings) is None


class TestFlap:
    """Repeated degradation must not hide behind automatic recovery (ADR-0002)."""

    def test_fires_on_repeated_degradations_within_the_window(self):
        transitions = [
            make_transition(
                from_state=RunState.HEALTHY,
                to_state=RunState.DEGRADED,
                created_at=T0 - timedelta(minutes=10 * i),
            )
            for i in range(3)
        ]
        ctx = context(make_run(state=RunState.DEGRADED), transitions=transitions, now=T0)
        assert rules.flap(ctx, SETTINGS) is not None

    def test_degradations_outside_the_window_do_not_count(self):
        transitions = [
            make_transition(
                from_state=RunState.HEALTHY,
                to_state=RunState.DEGRADED,
                created_at=T0 - timedelta(hours=2, minutes=i),
            )
            for i in range(5)
        ]
        ctx = context(make_run(state=RunState.DEGRADED), transitions=transitions, now=T0)
        assert rules.flap(ctx, SETTINGS) is None

    def test_other_transitions_are_not_degradations(self):
        """Only HEALTHY->DEGRADED counts. A release is not evidence of flapping."""
        transitions = [
            make_transition(
                from_state=RunState.FROZEN,
                to_state=RunState.HEALTHY,
                cause="release",
                actor="human:op",
                created_at=T0 - timedelta(minutes=i),
            )
            for i in range(5)
        ]
        ctx = context(transitions=transitions, now=T0)
        assert rules.flap(ctx, SETTINGS) is None


class TestFirstFiring:
    def test_returns_none_when_nothing_fires(self):
        assert rules.first_firing(context(proposed=call()), SETTINGS) is None

    def test_returns_the_first_rule_in_registry_order(self):
        """The recorded cause must be deterministic when several rules fire at once."""
        run = make_run(budget_tokens=10, tokens_used=99, consecutive_errors=99)
        verdict = rules.first_firing(context(run, proposed=call()), SETTINGS)
        assert verdict is not None
        assert verdict.rule == "budget"

    def test_every_rule_is_registered(self):
        """A rule that exists but is not in RULES is a detector that never runs."""
        defined = {
            name
            for name, obj in vars(rules).items()
            if callable(obj) and not name.startswith("_")
            and getattr(obj, "__module__", None) == rules.__name__
            and name not in {"first_firing", "Rule"}
        }
        assert {r.__name__ for r in rules.RULES} == defined
