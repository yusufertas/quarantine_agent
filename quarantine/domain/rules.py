"""Deterministic detectors (spec §7).

Every rule is a pure function of a RuleContext: no clock, no storage, no network.
That is the whole reason the LOW-risk gate path can stay fast, and it is why these
tests need no fixtures.

Rules are evaluated in RULES order; the first to fire wins and its name becomes the
recorded cause of the resulting transition.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import Settings
from .models import RuleContext, RuleVerdict

Rule = Callable[[RuleContext, Settings], "RuleVerdict | None"]


def budget(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """Tokens or spend past the run's registered budget."""
    raise NotImplementedError("rules.budget")


def loop(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """The same (tool, args) repeated within the recent window -- the stuck agent."""
    raise NotImplementedError("rules.loop")


def error_streak(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """Consecutive tool failures: an agent flailing against a broken dependency."""
    raise NotImplementedError("rules.error_streak")


def wall_clock(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """Past the run's deadline."""
    raise NotImplementedError("rules.wall_clock")


def call_rate(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """Tool calls per minute above threshold: thrashing."""
    raise NotImplementedError("rules.call_rate")


def flap(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """Repeated HEALTHY->DEGRADED within a window (ADR-0002).

    The odd one out: it reads transition history rather than run counters. It needs
    no special handling in the machine -- firing while a run is DEGRADED escalates it
    one rung to FROZEN, which is exactly the required behaviour, and firing also
    suppresses auto-recovery on the same pass. Without it, automatic recovery would
    convert intermittent misbehaviour into a permanently invisible problem.
    """
    raise NotImplementedError("rules.flap")


RULES: tuple[Rule, ...] = (budget, loop, error_streak, wall_clock, call_rate, flap)


def first_firing(ctx: RuleContext, settings: Settings) -> RuleVerdict | None:
    """The first rule in RULES order to fire, or None."""
    raise NotImplementedError("rules.first_firing")
