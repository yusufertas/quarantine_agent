"""The system as it ships: SDK -> HTTP -> gate -> SQLite, with the reaper.

Every other file tests a layer against a fake of its neighbour. The API is
tested over `InMemoryRepository`; the store is tested through its own methods;
the SDK is tested over a `FakeTransport`; the detectors are driven through the
API -- over the fake again. Nothing joined the real pieces: no test constructed
`create_app` over `SqliteRepository`, and no test ran `QuarantineClient` against
the real app except with the store broken. Two detectors once passed every unit
test while being dead in the assembled system; this file is the same lesson
applied to the assembly itself.

The transport is the README's own example, verbatim in shape, over
`TestClient`. Time is injected as everywhere else; the only real I/O is a
SQLite file under `tmp_path`.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import FakeClock, FakeJudge
from quarantine.api import create_app
from quarantine.config import Settings
from quarantine.domain.models import Verdict
from quarantine.domain.states import EventKind, RunState, Severity
from quarantine.errors import RunTerminated, UnknownRun
from quarantine.reaper import Reaper
from quarantine.sdk import QuarantineClient, RunFrozen, ToolRefused, TransportError
from quarantine.sqlite_store import SqliteRepository

OPERATOR = {"X-Operator-Token": "test-token", "X-Operator-Id": "operator-1"}
LOW = "search"
HIGH = "issue_payment"


class HttpTransport:
    """The shape the README documents, honouring both obligations."""

    def __init__(self, http: TestClient) -> None:
        self._http = http

    def post(self, path: str, body: dict) -> dict:
        response = self._http.post(path, json=body)
        if response.status_code >= 400:
            raise TransportError(response.status_code, response.text)
        return response.json()


class System:
    """One assembled control plane plus the handles a test needs."""

    def __init__(self, tmp_path, judge: FakeJudge, settings: Settings) -> None:
        self.path = str(tmp_path / "control-plane.db")
        self.store = SqliteRepository(self.path)
        self.store.initialize()
        self.clock = FakeClock()
        self.judge = judge
        self.settings = settings
        self.http = TestClient(
            create_app(store=self.store, judge=judge, clock=self.clock, settings=settings)
        )
        self.reaper = Reaper(store=self.store, clock=self.clock, settings=settings)

    def register(self, **overrides) -> str:
        body = {
            "agent_name": "invoice-bot",
            "budget_tokens": 100_000,
            "budget_cost_cents": 500,
            "deadline_seconds": 3_600,
        } | overrides
        response = self.http.post("/runs", json=body)
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def worker(self, run_id: str) -> QuarantineClient:
        return QuarantineClient(run_id=run_id, transport=HttpTransport(self.http))

    def detail(self, run_id: str) -> dict:
        response = self.http.get(f"/runs/{run_id}", headers=OPERATOR)
        assert response.status_code == 200, response.text
        return response.json()

    def trajectory(self, run_id: str) -> list[dict]:
        response = self.http.get(f"/runs/{run_id}/trajectory", headers=OPERATOR)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def judge() -> FakeJudge:
    return FakeJudge()


@pytest.fixture
def system(tmp_path, judge, settings) -> System:
    return System(tmp_path, judge, settings)


class TestTheReadmeWalkthrough:
    """The onboarding sequence in the README, end to end, on the real store."""

    def test_a_worker_is_gated_declawed_inspected_released_and_finished(
        self, system, judge
    ):
        run_id = system.register()
        worker = system.worker(run_id)

        # A permitted call runs, and its outcome lands in SQLite.
        assert worker.guard(LOW, "q1", lambda: "found") == "found"
        assert system.detail(run_id)["tool_calls"] == 1

        # The judge objects to a HIGH-risk call: refused, and the run is declawed.
        judge.verdict = Verdict(Severity.CONCERN, "paying an unknown account")
        paid = []
        with pytest.raises(ToolRefused):
            worker.guard(HIGH, "pay-unknown", lambda: paid.append("paid"))
        assert paid == [], "a refused call must never execute"
        assert system.detail(run_id)["state"] == "degraded"

        # DEGRADED keeps working on LOW-risk tools -- the point of the ladder.
        assert worker.guard(LOW, "q2", lambda: "fallback") == "fallback"

        # It flails against a broken dependency; the streak freezes it on the
        # next proposal, and the frozen run holds the call it was about to make.
        for _ in range(system.settings.error_streak):
            worker.report(LOW, ok=False)
        with pytest.raises(RunFrozen):
            worker.guard(LOW, "the-call-it-was-about-to-make", lambda: None)

        # What the operator sees: every cause, the judge's reason, and both
        # refused proposals -- the evidence the design exists to preserve.
        detail = system.detail(run_id)
        assert detail["state"] == "frozen"
        assert [t["cause"] for t in detail["transitions"]] == ["judge", "error_streak"]
        assert detail["transitions"][0]["detail"] == "paying an unknown account"
        proposed = {
            (e["tool_name"], e["args_digest"])
            for e in system.trajectory(run_id)
            if e["kind"] == "proposed"
        }
        assert (HIGH, "pay-unknown") in proposed
        assert (LOW, "the-call-it-was-about-to-make") in proposed

        # A human releases it to HEALTHY; the judge is content; payment proceeds.
        response = system.http.post(
            f"/runs/{run_id}/release", json={"target": "healthy"}, headers=OPERATOR
        )
        assert response.status_code == 200, response.text
        assert system.detail(run_id)["transitions"][-1]["actor"] == "human:operator-1"
        judge.verdict = Verdict(Severity.CLEAR, "on task")
        worker.report(LOW, ok=True)          # clears the streak
        assert worker.guard(HIGH, "pay-known", lambda: "paid") == "paid"

        # The worker finishes; the run leaves the ladder and accepts nothing more.
        worker.complete()
        assert system.detail(run_id)["state"] == "terminated"
        with pytest.raises(RunTerminated):
            worker.guard(LOW, "q3", lambda: "too late")

    def test_an_unknown_run_reaches_the_worker_as_the_typed_error(self, system):
        """The real 404, through the real transport, arrives as `UnknownRun`
        -- not as the transport's exception and not as a decision."""
        worker = system.worker("never-registered")
        with pytest.raises(UnknownRun):
            worker.guard(LOW, "q1", lambda: "result")


class TestPersistenceThroughTheApi:
    def test_state_survives_a_restart(self, system, judge):
        """A second repository over the same file sees what the first wrote --
        the property that makes a control-plane restart safe, and one no test
        over the in-memory fake can express."""
        run_id = system.register()
        judge.verdict = Verdict(Severity.SEVERE, "exfiltrating")
        with pytest.raises(ToolRefused):
            system.worker(run_id).guard(HIGH, "d1", lambda: None)

        reopened = SqliteRepository(system.path)
        run = reopened.get_run(run_id)
        assert run.state is RunState.DEGRADED
        assert [t.cause for t in reopened.transitions(run_id)] == ["judge"]
        kinds = [e.kind for e in reopened.recent_events(run_id, 10)]
        assert kinds == [
            EventKind.PROPOSED,
            EventKind.JUDGE,
            EventKind.DECISION,
            EventKind.OUTCOME,
        ] or kinds[:3] == [EventKind.PROPOSED, EventKind.JUDGE, EventKind.DECISION]

    def test_outcome_reports_accumulate_on_the_real_row(self, system):
        run_id = system.register()
        worker = system.worker(run_id)
        worker.report(LOW, ok=True, tokens=120, cost_cents=3)
        worker.report(LOW, ok=False, tokens=80, cost_cents=2)
        detail = system.detail(run_id)
        assert detail["tokens_used"] == 200
        assert detail["cost_cents"] == 5
        assert detail["tool_calls"] == 2
        assert detail["consecutive_errors"] == 1

    def test_a_late_heartbeat_does_not_revive_a_terminated_run(self, system):
        """The storage-level invariant, reached the way production reaches it:
        `save_run` through the heartbeat route carries no `state`."""
        run_id = system.register()
        worker = system.worker(run_id)
        worker.complete()
        worker.heartbeat()
        assert system.detail(run_id)["state"] == "terminated"


class TestDetectorsOnTheRealStore:
    """`test_detector_integration.py` drives the detectors through the API --
    over the fake. `recent_events` ordering and `seq` allocation are the two
    things SQLite does differently, and both feed the `loop` window."""

    def test_the_loop_detector_fires(self, system):
        run_id = system.register()
        worker = system.worker(run_id)
        for _ in range(2):
            worker.guard(LOW, "identical", lambda: None)
        # The third identical proposal trips it: refused? No -- LOW-risk calls
        # are still ALLOWED in DEGRADED. The run is declawed, not stopped.
        worker.guard(LOW, "identical", lambda: None)
        detail = system.detail(run_id)
        assert detail["state"] == "degraded"
        assert [t["cause"] for t in detail["transitions"]] == ["loop"]

    def test_the_budget_detector_fires_on_reported_spend(self, system):
        run_id = system.register(budget_tokens=100)
        worker = system.worker(run_id)
        worker.report(LOW, ok=True, tokens=101)
        with pytest.raises(ToolRefused):
            worker.guard(HIGH, "d1", lambda: None)
        detail = system.detail(run_id)
        assert detail["state"] == "degraded"
        assert detail["transitions"][0]["cause"] == "budget"

    def test_the_call_rate_detector_fires(self, tmp_path):
        """The detector that was decorative for ten reviews, on the real store.

        A frozen clock puts every call inside the trailing minute. Distinct
        digests keep `loop` quiet, so this escalation has to be `call_rate`.
        """
        rate = 10
        system = System(
            tmp_path, FakeJudge(), Settings(operator_token="test-token", call_rate_per_minute=rate)
        )
        run_id = system.register(budget_tokens=10**9, budget_cost_cents=10**9)
        worker = system.worker(run_id)
        # Once over the threshold every further call re-fires the rule (the
        # window still holds the burst), so the run walks to FROZEN and the SDK
        # starts raising; the burst continues regardless, as a thrashing agent's
        # would.
        for i in range(rate * 2):
            with suppress(RunFrozen):
                worker.guard(LOW, f"d{i}", lambda: None)
        detail = system.detail(run_id)
        assert detail["state"] == "frozen"
        assert [t["cause"] for t in detail["transitions"]] == ["call_rate", "call_rate"]

    def test_auto_recovery_reads_state_since_from_the_row(self, system):
        """`state_since` round-trips through ISO text; a naive/aware mismatch
        here would raise inside the gate on every DEGRADED call.

        Degraded by `error_streak` rather than `loop`, because a loop stays in
        the window and re-fires on the next call; a cleared streak does not.
        """
        run_id = system.register()
        worker = system.worker(run_id)
        for _ in range(system.settings.error_streak):
            worker.report(LOW, ok=False)
        worker.guard(LOW, "d1", lambda: None)
        assert system.detail(run_id)["state"] == "degraded"

        worker.report(LOW, ok=True)
        system.clock.advance(system.settings.recovery_interval + timedelta(seconds=1))
        worker.guard(LOW, "d2", lambda: None)
        detail = system.detail(run_id)
        assert detail["state"] == "healthy"
        assert [t["cause"] for t in detail["transitions"]] == ["error_streak", "clean_interval"]


class TestTheReaperOverTheRealStore:
    """`runs_with_stale_heartbeat` compares ISO-8601 text in SQL. It only works
    because every timestamp is written in one format; a naive datetime, or one
    in another zone, would sort wrongly and silently. The reaper is the only
    caller, so this is where the comparison gets exercised end to end."""

    def test_a_silent_worker_is_frozen_in_two_sweeps_and_told_so(self, system):
        run_id = system.register()
        worker = system.worker(run_id)
        worker.guard(LOW, "d1", lambda: None)

        timeout = system.settings.heartbeat_timeout + timedelta(seconds=1)
        system.clock.advance(timeout)
        assert system.reaper.sweep() == 1
        assert system.detail(run_id)["state"] == "degraded"

        system.clock.advance(timeout)
        assert system.reaper.sweep() == 1
        assert system.detail(run_id)["state"] == "frozen"

        with pytest.raises(RunFrozen):
            worker.guard(LOW, "d2", lambda: None)
        causes = [t["cause"] for t in system.detail(run_id)["transitions"]]
        assert causes == ["heartbeat_timeout", "heartbeat_timeout"]

    def test_a_heartbeat_keeps_the_reaper_away(self, system):
        run_id = system.register()
        worker = system.worker(run_id)
        timeout = system.settings.heartbeat_timeout
        system.clock.advance(timeout - timedelta(seconds=1))
        worker.heartbeat()
        system.clock.advance(timeout - timedelta(seconds=1))
        assert system.reaper.sweep() == 0
        assert system.detail(run_id)["state"] == "healthy"

    def test_the_sweep_only_touches_stale_runs(self, system):
        stale = system.register(agent_name="quiet")
        live = system.register(agent_name="chatty")
        system.clock.advance(system.settings.heartbeat_timeout + timedelta(seconds=1))
        system.worker(live).heartbeat()
        assert system.reaper.sweep() == 1
        assert system.detail(stale)["state"] == "degraded"
        assert system.detail(live)["state"] == "healthy"


class TestTheOperatorListOverTheRealStore:
    def test_list_filters_and_orders_by_registration(self, system):
        first = system.register(agent_name="first")
        system.clock.advance(timedelta(seconds=1))
        second = system.register(agent_name="second")
        system.worker(second).complete()

        everything = system.http.get("/runs", headers=OPERATOR).json()
        assert [r["id"] for r in everything] == [first, second]
        terminated = system.http.get(
            "/runs", params={"state": "terminated"}, headers=OPERATOR
        ).json()
        assert [r["id"] for r in terminated] == [second]
