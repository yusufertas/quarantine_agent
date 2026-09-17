"""Exceptions that carry design meaning.

Each of these corresponds to a row in the spec's failure-handling table; none of
them is a generic wrapper. Catching them broadly defeats the point.
"""


class QuarantineError(Exception):
    """Base for everything this package raises deliberately."""


class UnknownRun(QuarantineError):
    """No such run. Never degrades into a permissive default (spec §9)."""


class RunTerminated(QuarantineError):
    """Gate called against a TERMINATED run. An error, never a FREEZE.

    Carries a sentence rather than a bare id: this renders straight into the
    409 body, and `{"detail": "<uuid>"}` tells the worker developer reading it
    nothing about what went wrong.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(
            f"run {run_id} is TERMINATED; it accepts no further tool calls"
        )


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
    """Storage is unreachable; the control plane cannot decide (spec §9).

    Raised at the repository boundary and surfaced as `503` -- deliberately not
    `500`, because the worker SDK tiers on it exactly as it tiers on an
    unreachable control plane: `HIGH` blocked, `LOW` proceeds. A `500` would be
    indistinguishable from a bug and would crash the agent instead.
    """
