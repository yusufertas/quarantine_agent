"""The gate: the hot path, and the heart of the system (spec §5).

Every other component exists to serve this decision. The LOW-risk branch must stay
free of I/O -- the tiering in ADR-0003 is pointless the moment it acquires a model
call or a network hop.
"""

from __future__ import annotations

from .config import Settings
from .domain.models import ToolCall
from .domain.states import Decision


class Gate:
    def __init__(self, store, judge, clock, settings: Settings) -> None:
        self._store = store
        self._judge = judge
        self._clock = clock
        self._settings = settings

    def decide(self, run_id: str, call: ToolCall) -> Decision:
        """Answer ALLOW / DENY / FREEZE for a proposed tool call.

        Order matters and is specified:

          1. Unknown run -> UnknownRun; TERMINATED run -> RunTerminated. Neither
             degrades into a permissive default or a FREEZE, because both would hide
             a bug in the caller.
          2. Already FROZEN -> FREEZE immediately.
          3. Evaluate rules. A firing rule escalates one rung. If no rule fires and
             the run has been DEGRADED longer than the recovery interval, it
             auto-recovers to HEALTHY.
          4. Resolve the tool's risk; unclassified is HIGH.
          5. DEGRADED + HIGH -> DENY without consulting the judge: the answer cannot
             change, so paying for it would be waste.
          6. HIGH -> block on the judge. JudgeUnavailable -> DENY (fail closed).
             CONCERN or worse escalates one rung, then FREEZE or DENY accordingly.
          7. Otherwise ALLOW. LOW-risk trajectories are judged out of band.

        Every call appends a PROPOSED event and a DECISION event, whatever the answer.
        """
        raise NotImplementedError("Gate.decide")

    def _maybe_judge_in_background(self, run_id: str) -> None:
        """Queue out-of-band judging every `judge_background_every` gate calls."""
        raise NotImplementedError("Gate._maybe_judge_in_background")
