"""Detectors driven end to end, through real gate calls.

The reason this file exists, separately from `test_rules.py`:

`test_rules.py` hands each rule a `RuleContext` it builds itself. That proves the
rule's arithmetic and nothing about whether the gate can ever assemble a context
in which the rule fires. `call_rate` passed its unit tests for ten reviews while
being incapable of firing in production, because the gate's history window is
measured in LOG ROWS and the rule counts TOOL CALLS -- and every call writes
several rows. A window of `call_rate_per_minute + 1` rows could hold at most a
quarter that many proposals, so the detector was decorative: exactly the failure
mode the PRD's risk table names.

So these tests drive the real HTTP surface at a real rate and assert the run
actually escalates. If the events-per-call ratio drifts again -- another event
kind on the hot path, a changed window -- these fail. No synthetic context can
catch that, which is precisely how the defect survived.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import FakeJudge
from quarantine.config import Settings
from quarantine.api import create_app
from quarantine.domain.states import EventKind, RunState
from quarantine.gate import Gate

# Low enough to run in a blink, high enough that `call_rate_per_minute + 1` is the
# term that dominates the history window -- as it does at the production default of
# 60, and unlike a threshold small enough to hide behind `judge_trajectory_events`.
RATE = 20


@pytest.fixture
def settings() -> Settings:
    return Settings(operator_token="test-token", call_rate_per_minute=RATE)


@pytest.fixture
def client(store, clock, settings) -> TestClient:
    return TestClient(
        create_app(store=store, judge=FakeJudge(), clock=clock, settings=settings)
    )


def register(client: TestClient) -> str:
    response = client.post(
        "/runs",
        json={
            "agent_name": "thrashing-agent",
            # Generous, so that `budget` and `wall_clock` cannot fire first and
            # steal the escalation this test is about.
            "budget_tokens": 10_000_000,
            "budget_cost_cents": 10_000_000,
            "deadline_seconds": 86_400,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def drive(client: TestClient, run_id: str, calls: int, *, digest_of=None) -> None:
    """One realistic worker turn per iteration: gate, then report the outcome.

    Distinct `args_digest` values by default so `loop` cannot fire -- this has to
    be `call_rate` or it proves nothing. `ok=True` keeps `error_streak` quiet too.
    """
    digest_of = digest_of or (lambda i: f"d{i}")
    for i in range(calls):
        client.post(
            f"/runs/{run_id}/gate",
            json={"tool_name": "search", "args_digest": digest_of(i)},
        )
        client.post(
            f"/runs/{run_id}/outcome", json={"tool_name": "search", "ok": True}
        )


class TestCallRateFiresInPractice:
    """The clock is frozen, so every call lands inside the trailing minute --
    an infinite call rate, which is the shape of the real thrashing agent."""

    def test_a_run_calling_far_above_the_threshold_is_escalated(
        self, client, store
    ):
        run_id = register(client)
        drive(client, run_id, calls=RATE * 3)
        assert store.get_run(run_id).state is not RunState.HEALTHY

    def test_the_recorded_cause_is_call_rate(self, client, store):
        """Not merely 'something escalated it' -- the operator reads this cause."""
        run_id = register(client)
        drive(client, run_id, calls=RATE * 3)
        causes = [t.cause for t in store.transitions(run_id)]
        assert causes and causes[0] == "call_rate", causes

    def test_the_gate_sees_more_proposals_than_the_threshold(
        self, client, store, clock, settings
    ):
        """The mechanism, stated directly: the history window the gate passes to
        the rules must be able to hold more than `call_rate_per_minute` PROPOSED
        rows. When it could not, everything above was unreachable."""
        run_id = register(client)
        drive(client, run_id, calls=RATE * 3)
        limit = Gate(
            store=store, judge=FakeJudge(), clock=clock, settings=settings
        )._history_limit()
        window = store.recent_events(run_id, limit)
        proposals = [e for e in window if e.kind is EventKind.PROPOSED]
        assert len(proposals) > RATE, (
            f"{len(proposals)} proposals visible in a {len(window)}-row window; "
            f"the detector needs more than {RATE} to fire at all"
        )

    def test_a_run_below_the_threshold_is_left_alone(self, client, store):
        """The other half: a window sized for the amplification must not turn
        the detector into a hair trigger."""
        run_id = register(client)
        drive(client, run_id, calls=RATE - 5)
        assert store.get_run(run_id).state is RunState.HEALTHY


class TestLoopFiresInPractice:
    """`loop`'s window is thinner than it reads for the same reason -- ten rows,
    not ten calls -- but it still fires at the spec's default threshold of three.
    Pinned here so a change to the events-per-call ratio is caught for this rule
    too, rather than being noticed years later like `call_rate` was."""

    def test_an_agent_repeating_one_call_is_escalated(self, client, store):
        run_id = register(client)
        drive(client, run_id, calls=3, digest_of=lambda _: "identical")
        assert store.get_run(run_id).state is RunState.DEGRADED
        assert [t.cause for t in store.transitions(run_id)] == ["loop"]

    def test_varied_calls_do_not_trip_it(self, client, store):
        run_id = register(client)
        drive(client, run_id, calls=3)
        assert store.get_run(run_id).state is RunState.HEALTHY
