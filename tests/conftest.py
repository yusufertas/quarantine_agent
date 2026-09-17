"""Shared fakes and builders.

Time and the judge are injected everywhere, so nothing here sleeps and nothing
reaches a model or a database (spec §11).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from quarantine.config import Settings
from quarantine.domain.models import Event, Run, ToolCall, Transition, Verdict
from quarantine.domain.states import EventKind, RunState, Severity
from quarantine.errors import JudgeUnavailable, UnknownRun

T0 = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """A clock the test drives. The reason heartbeat tests take microseconds."""

    def __init__(self, now: datetime = T0) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class FakeJudge:
    """Configurable judge. Records calls so tests can assert it was NOT consulted."""

    def __init__(self, verdict: Verdict | None = None, raises: Exception | None = None):
        self.verdict = verdict or Verdict(Severity.CLEAR, "nothing of concern")
        self.raises = raises
        self.calls: list[str] = []

    def evaluate(self, run: Run, trajectory: Sequence[Event]) -> Verdict:
        self.calls.append(run.id)
        if self.raises is not None:
            raise self.raises
        return self.verdict


class UnavailableJudge(FakeJudge):
    def __init__(self) -> None:
        super().__init__(raises=JudgeUnavailable("timed out"))


class InMemoryRepository:
    """Repository implementation for tests. Append-only where the protocol is."""

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self.events: list[Event] = []
        self.transitions_log: list[Transition] = []

    def create_run(self, run: Run) -> None:
        self.runs[run.id] = run

    def get_run(self, run_id: str) -> Run:
        try:
            return self.runs[run_id]
        except KeyError:
            raise UnknownRun(run_id) from None

    def save_run(self, run: Run) -> None:
        self.runs[run.id] = run

    def list_runs(self, state: RunState | None = None) -> Sequence[Run]:
        runs = list(self.runs.values())
        return [r for r in runs if state is None or r.state is state]

    def append_event(self, event: Event) -> None:
        self.events.append(event)

    def recent_events(self, run_id: str, limit: int) -> Sequence[Event]:
        return [e for e in self.events if e.run_id == run_id][-limit:]

    def next_seq(self, run_id: str) -> int:
        return sum(1 for e in self.events if e.run_id == run_id) + 1

    def append_transition(self, transition: Transition) -> None:
        self.transitions_log.append(transition)

    def transitions(self, run_id: str) -> Sequence[Transition]:
        return [t for t in self.transitions_log if t.run_id == run_id]

    def runs_with_stale_heartbeat(self, cutoff: datetime) -> Sequence[Run]:
        return [
            r
            for r in self.runs.values()
            if r.last_heartbeat_at < cutoff and r.state is not RunState.TERMINATED
        ]


def make_run(
    run_id: str = "run-1",
    state: RunState = RunState.HEALTHY,
    *,
    now: datetime = T0,
    state_since: datetime | None = None,
    last_heartbeat_at: datetime | None = None,
    budget_tokens: int = 100_000,
    budget_cost_cents: int = 1_000,
    deadline_after: timedelta = timedelta(hours=1),
    **counters,
) -> Run:
    return replace(
        Run(
            id=run_id,
            agent_name="test-agent",
            state=state,
            state_since=state_since or now,
            created_at=now,
            last_heartbeat_at=last_heartbeat_at or now,
            budget_tokens=budget_tokens,
            budget_cost_cents=budget_cost_cents,
            deadline_at=now + deadline_after,
        ),
        **counters,
    )


def make_event(
    run_id: str = "run-1",
    seq: int = 1,
    *,
    kind: EventKind = EventKind.PROPOSED,
    tool_name: str | None = "search",
    args_digest: str | None = "d1",
    created_at: datetime = T0,
    **payload,
) -> Event:
    return Event(
        run_id=run_id,
        seq=seq,
        kind=kind,
        created_at=created_at,
        tool_name=tool_name,
        args_digest=args_digest,
        payload=payload,
    )


def make_transition(
    run_id: str = "run-1",
    *,
    from_state: RunState = RunState.HEALTHY,
    to_state: RunState = RunState.DEGRADED,
    cause: str = "loop",
    actor: str = "system",
    created_at: datetime = T0,
) -> Transition:
    return Transition(
        run_id=run_id,
        from_state=from_state,
        to_state=to_state,
        cause=cause,
        actor=actor,
        created_at=created_at,
    )


def call(tool_name: str = "search", args_digest: str = "d1") -> ToolCall:
    return ToolCall(tool_name=tool_name, args_digest=args_digest)


@pytest.fixture
def settings() -> Settings:
    return Settings(operator_token="test-token")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def judge() -> FakeJudge:
    return FakeJudge()
