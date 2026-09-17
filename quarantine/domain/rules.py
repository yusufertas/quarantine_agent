"""Deterministic detectors (spec §7).

Every rule is a pure function of a RuleContext: no clock, no storage, no network.
That is the whole reason the LOW-risk gate path can stay fast, and it is why these
tests need no fixtures.

Rules are evaluated in RULES order; the first to fire wins and its name becomes the
recorded cause of the resulting transition.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import timedelta

from ..config import Settings
from .models import RuleContext, RuleVerdict
from .states import EventKind, RunState

Rule = Callable[[RuleContext, Settings], "RuleVerdict | None"]


def budget(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """Tokens or spend past the run's registered budget."""
    run = ctx.run
    if run.tokens_used > run.budget_tokens:
        return RuleVerdict(
            "budget", f"{run.tokens_used} tokens against a budget of {run.budget_tokens}"
        )
    if run.cost_cents > run.budget_cost_cents:
        return RuleVerdict(
            "budget", f"{run.cost_cents}c spent against a budget of {run.budget_cost_cents}c"
        )
    return None


def loop(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """The same (tool, args) repeated within the recent window -- the stuck agent."""
    window = ctx.recent_events[-settings.loop_window :]
    signatures = [
        (e.tool_name, e.args_digest)
        for e in window
        if e.kind is EventKind.PROPOSED and e.tool_name
    ]
    if ctx.call is not None:
        signatures.append((ctx.call.tool_name, ctx.call.args_digest))
    if not signatures:
        return None
    (tool_name, _), count = Counter(signatures).most_common(1)[0]
    if count >= settings.loop_repeats:
        return RuleVerdict("loop", f"{tool_name} called {count}x with identical arguments")
    return None


def error_streak(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """Consecutive tool failures: an agent flailing against a broken dependency."""
    if ctx.run.consecutive_errors >= settings.error_streak:
        return RuleVerdict(
            "error_streak", f"{ctx.run.consecutive_errors} consecutive tool failures"
        )
    return None


def wall_clock(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """Past the run's deadline."""
    if ctx.now > ctx.run.deadline_at:
        return RuleVerdict(
            "wall_clock", f"past the run deadline of {ctx.run.deadline_at.isoformat()}"
        )
    return None


def call_rate(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """Tool calls per minute above threshold: thrashing."""
    cutoff = ctx.now - timedelta(minutes=1)
    recent = [
        e
        for e in ctx.recent_events
        if e.kind is EventKind.PROPOSED and e.created_at >= cutoff
    ]
    if len(recent) > settings.call_rate_per_minute:
        return RuleVerdict("call_rate", f"{len(recent)} tool calls in the trailing minute")
    return None


def flap(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """Repeated HEALTHY->DEGRADED within a window (ADR-0002).

    The odd one out: it reads transition history rather than run counters. It needs
    no special handling in the machine -- firing while a run is DEGRADED escalates it
    one rung to FROZEN, which is exactly the required behaviour, and firing also
    suppresses auto-recovery on the same pass. Without it, automatic recovery would
    convert intermittent misbehaviour into a permanently invisible problem.
    """
    cutoff = ctx.now - settings.flap_window
    degradations = [
        t
        for t in ctx.recent_transitions
        if t.from_state is RunState.HEALTHY
        and t.to_state is RunState.DEGRADED
        and t.created_at >= cutoff
    ]
    if len(degradations) >= settings.flap_degradations:
        return RuleVerdict(
            "flap", f"{len(degradations)} degradations within {settings.flap_window}"
        )
    return None


RULES: tuple[Rule, ...] = (budget, loop, error_streak, wall_clock, call_rate, flap)


def first_firing(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """The first rule in RULES order to fire, or None."""
    for rule in RULES:
        verdict = rule(ctx, settings)
        if verdict is not None:
            return verdict
    return None
