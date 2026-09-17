"""The gate: the hot path, and the heart of the system (spec §5).

Every other component exists to serve this decision. The LOW-risk branch must stay
free of I/O -- the tiering in ADR-0003 is pointless the moment it acquires a model
call or a network hop.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .config import Settings
from .domain import machine, registry, rules
from .domain.models import Event, RuleContext, ToolCall
from .domain.states import Decision, EventKind, RunState, Severity, ToolRisk
from .errors import JudgeUnavailable, RunTerminated


class Gate:
    def __init__(
        self,
        store,
        judge,
        clock,
        settings: Settings,
        background: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._store = store
        self._judge = judge
        self._clock = clock
        self._settings = settings
        self._background = background

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
        run = self._store.get_run(run_id)          # raises UnknownRun
        if run.state is RunState.TERMINATED:
            raise RunTerminated(run_id)

        now = self._clock.now()

        # Snapshot the trajectory BEFORE recording this proposal, so the loop
        # rule sees the proposed call exactly once -- via ctx.call, not twice.
        history = tuple(self._store.recent_events(run_id, self._history_limit()))
        self._append(
            run_id,
            EventKind.PROPOSED,
            now,
            tool_name=call.tool_name,
            args_digest=call.args_digest,
            payload={"args_preview": call.args_preview},
        )

        if run.state is RunState.FROZEN:
            return self._record(run_id, now, call, Decision.FREEZE)

        run = self._apply_rules(run, call, history, now)

        if run.state is RunState.FROZEN:
            return self._record(run_id, now, call, Decision.FREEZE)

        risk = registry.risk_of(call.tool_name)

        # The answer here cannot change, so paying the judge for it is waste.
        if run.state is RunState.DEGRADED and risk is ToolRisk.HIGH:
            return self._record(run_id, now, call, Decision.DENY)

        if risk is ToolRisk.HIGH:
            decision = self._consult_judge(run, run_id, call, now)
            if decision is not None:
                return decision

        self._maybe_judge_in_background(run_id)
        return self._record(run_id, now, call, Decision.ALLOW)

    def _apply_rules(self, run, call, history, now):
        """Escalate on a firing rule, else auto-recover if the interval has elapsed."""
        ctx = RuleContext(
            run=run,
            call=call,
            recent_events=history,
            recent_transitions=tuple(self._store.transitions(run.id)),
            now=now,
        )
        fired = rules.first_firing(ctx, self._settings)
        if fired is not None:
            return self._transition(
                machine.escalate(
                    run, cause=fired.rule, actor="system", now=now, detail=fired.detail
                )
            )
        if (
            run.state is RunState.DEGRADED
            and now - run.state_since >= self._settings.recovery_interval
        ):
            return self._transition(machine.auto_recover(run, now=now))
        return run

    def _consult_judge(self, run, run_id, call, now) -> Decision | None:
        """Block on the judge. Returns a Decision to short-circuit, or None to continue."""
        trajectory = self._store.recent_events(
            run_id, self._settings.judge_trajectory_events
        )
        try:
            verdict = self._judge.evaluate(run, trajectory)
        except JudgeUnavailable as exc:
            # Never swallowed: a silently-caught timeout is a system that looks
            # like it is judging and is not.
            self._append(
                run_id, EventKind.JUDGE, now, payload={"available": False, "error": str(exc)}
            )
            return self._record(run_id, now, call, Decision.DENY)   # fail closed

        self._append(
            run_id,
            EventKind.JUDGE,
            now,
            payload={"severity": verdict.severity.value, "reason": verdict.reason},
        )
        if verdict.severity >= Severity.CONCERN:
            self._transition(
                machine.escalate(
                    run, cause="judge", actor="system", now=now, detail=verdict.reason
                )
            )
            # Only reachable from HEALTHY, so the run is now DEGRADED and this
            # HIGH-risk call is refused. A FREEZE here would be unreachable code.
            return self._record(run_id, now, call, Decision.DENY)
        return None

    def _transition(self, outcome):
        # One call, one transaction: the new state and its audit row land together
        # or not at all, and nothing here can write a counter back from a stale
        # snapshot (spec §4, invariant 4).
        run, transition = outcome
        self._store.record_state_change(run, transition)
        return run

    def _record(self, run_id, now, call, decision: Decision) -> Decision:
        self._append(
            run_id,
            EventKind.DECISION,
            now,
            tool_name=call.tool_name,
            args_digest=call.args_digest,
            payload={"decision": decision.value},
        )
        return decision

    def _append(
        self,
        run_id: str,
        kind: EventKind,
        now: datetime,
        *,
        tool_name: str | None = None,
        args_digest: str | None = None,
        payload: dict | None = None,
    ) -> None:
        self._store.append_event(
            Event(
                run_id=run_id,
                seq=self._store.next_seq(run_id),
                kind=kind,
                created_at=now,
                tool_name=tool_name,
                args_digest=args_digest,
                payload=payload or {},
            )
        )

    # Every gate call writes at least two log rows -- PROPOSED and DECISION -- and
    # commonly four, once the worker reports an OUTCOME and the judge appends a
    # JUDGE row. Call this the events-per-call amplification.
    #
    # The rules think in TOOL CALLS; `recent_events` is measured in LOG ROWS. Without
    # this factor a window of `call_rate_per_minute + 1` rows can never contain more
    # than about a quarter that many PROPOSED events, so `call_rate` -- whose whole
    # job is to fire above that threshold -- could not fire at any rate whatsoever.
    # It looked like protection and provided none.
    #
    # Do NOT "simplify" the multiplier away: the window must be at least
    # amplification x threshold rows for the detector to be able to see a breach.
    # tests/test_detector_integration.py drives real gate calls end to end and fails
    # if the ratio drifts again; a unit test over a hand-built context cannot.
    _EVENTS_PER_CALL = 4

    def _history_limit(self) -> int:
        s = self._settings
        return max(
            s.loop_window,
            s.judge_trajectory_events,
            self._EVENTS_PER_CALL * (s.call_rate_per_minute + 1),
        )

    def _maybe_judge_in_background(self, run_id: str) -> None:
        """Queue out-of-band judging every `judge_background_every` gate calls.

        With no scheduler injected there is no background judging -- which is
        why the LOW-risk path in tests performs no I/O at all. The API wires a
        real scheduler in Task 7.
        """
        if self._background is None:
            return
        if self._store.next_seq(run_id) % self._settings.judge_background_every != 0:
            return
        self._background(lambda: self._judge_now(run_id))

    def _judge_now(self, run_id: str) -> None:
        run = self._store.get_run(run_id)
        now = self._clock.now()
        trajectory = self._store.recent_events(
            run_id, self._settings.judge_trajectory_events
        )
        try:
            verdict = self._judge.evaluate(run, trajectory)
        except JudgeUnavailable as exc:
            self._append(
                run_id, EventKind.JUDGE, now, payload={"available": False, "error": str(exc)}
            )
            return
        self._append(
            run_id,
            EventKind.JUDGE,
            now,
            payload={"severity": verdict.severity.value, "reason": verdict.reason},
        )
        if verdict.severity >= Severity.CONCERN and run.state in machine.ESCALATIONS:
            self._transition(
                machine.escalate(
                    run, cause="judge", actor="system", now=now, detail=verdict.reason
                )
            )
