"""The LLM judge (spec §8).

Binding, not advisory: a CONCERN or SEVERE verdict escalates one rung exactly as a
rule firing does. The ladder is what makes a wrong verdict affordable -- the cost of
a hallucinating judge is a DEGRADED run, not a dead one (ADR-0002).

Where it runs is decided by the tool's blast radius, not by the judge: HIGH-risk
calls block on it, LOW-risk trajectories are judged out of band (ADR-0003).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .domain.models import Event, Run, Verdict


class Judge(Protocol):
    def evaluate(self, run: Run, trajectory: Sequence[Event]) -> Verdict:
        """Classify a run's recent trajectory.

        Raises JudgeUnavailable on timeout or transport failure -- never returns a
        CLEAR verdict to paper over an outage, which would make the system appear to
        be judging when it is not.
        """


class ClaudeJudge:
    """Judge backed by the Claude API. No unit test constructs a real one."""

    def __init__(self, client=None, timeout_seconds: float = 10.0) -> None:
        self._client = client
        self._timeout = timeout_seconds

    def evaluate(self, run: Run, trajectory: Sequence[Event]) -> Verdict:
        raise NotImplementedError("ClaudeJudge.evaluate")
