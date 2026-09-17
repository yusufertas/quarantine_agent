"""Exceptions that carry design meaning.

Each of these corresponds to a row in the spec's failure-handling table; none of
them is a generic wrapper. Catching them broadly defeats the point.
"""


class QuarantineError(Exception):
    """Base for everything this package raises deliberately."""


class UnknownRun(QuarantineError):
    """No such run. Never degrades into a permissive default (spec §9)."""


class RunTerminated(QuarantineError):
    """Gate called against a TERMINATED run. An error, never a FREEZE."""


class IllegalTransition(QuarantineError):
    """A state change not present in the transition table (spec §4)."""

    def __init__(self, source, target, reason: str = "") -> None:
        self.source = source
        self.target = target
        detail = f"{source} -> {target}"
        super().__init__(f"{detail}: {reason}" if reason else detail)


class AuthorizationRequired(QuarantineError):
    """A human actor is required for this operation (spec §4, invariant 5)."""


class JudgeUnavailable(QuarantineError):
    """The judge could not be reached or timed out. Triggers the §9 tiering."""


class StoreUnavailable(QuarantineError):
    """Storage is unreachable; the control plane cannot decide."""
