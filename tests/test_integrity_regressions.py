"""Audit regressions against the shipped HTTP and SQLite paths, without model calls."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from threading import Barrier

from fastapi.testclient import TestClient
import pytest

from conftest import FakeClock, FakeJudge, T0, make_run
from quarantine.api import create_app
from quarantine.config import Settings
from quarantine.domain import machine
from quarantine.domain.models import ToolCall, Verdict
from quarantine.domain.states import Decision, EventKind, RunState, Severity
from quarantine.errors import QuarantineError, RunTerminated
from quarantine.gate import Gate
from quarantine.sqlite_store import SqliteRepository


@pytest.fixture
def repo(tmp_path):
    store = SqliteRepository(str(tmp_path / "integrity.db"))
    store.initialize()
    store.create_run(make_run())
    return store


@pytest.fixture
def client(repo):
    with TestClient(create_app(repo, FakeJudge(), FakeClock(), Settings())) as http:
        yield http


@pytest.mark.parametrize("field", ["tokens", "cost_cents"])
@pytest.mark.parametrize("value", [-1, True, 2**63])
def test_invalid_usage_is_rejected_without_mutation(client, repo, field, value):
    before = repo.get_run("run-1")
    response = client.post(
        "/runs/run-1/outcome", json={"tool_name": "search", "ok": True, field: value}
    )
    assert response.status_code == 422
    assert repo.get_run("run-1") == before
    assert repo.recent_events("run-1", 100) == []


@pytest.mark.parametrize("field,value", [
    ("budget_tokens", -1), ("budget_cost_cents", -1),
    ("budget_tokens", 2**63), ("deadline_seconds", 0),
    ("deadline_seconds", -1), ("deadline_seconds", 10**30),
    ("deadline_seconds", 2**63 - 1),
])
def test_invalid_registration_does_not_create_a_run(client, repo, field, value):
    response = client.post("/runs", json={
        "agent_name": "test", "budget_tokens": 100, "budget_cost_cents": 100,
        "deadline_seconds": 3600, field: value,
    })
    assert response.status_code == 422
    assert len(repo.list_runs()) == 1


def test_zero_usage_and_zero_budget_are_valid(client, repo):
    response = client.post("/runs", json={
        "agent_name": "no-spend", "budget_tokens": 0, "budget_cost_cents": 0,
        "deadline_seconds": 1,
    })
    assert response.status_code == 201
    result = client.post(f"/runs/{response.json()['id']}/outcome", json={
        "tool_name": "search", "ok": True, "tokens": 0, "cost_cents": 0,
    })
    assert result.status_code == 200


@pytest.mark.parametrize("field,counter", [
    ("tokens", "tokens_used"), ("cost_cents", "cost_cents"),
])
def test_accumulated_usage_overflow_refuses_without_partial_write(client, repo, field, counter):
    body = {"tool_name": "search", "ok": True, field: 2**63 - 1}
    assert client.post("/runs/run-1/outcome", json=body).status_code == 200
    before = repo.get_run("run-1")
    assert getattr(before, counter) == 2**63 - 1
    response = client.post("/runs/run-1/outcome", json={**body, field: 1})
    assert response.status_code == 422
    assert repo.get_run("run-1") == before
    assert len(repo.recent_events("run-1", 100)) == 1


def test_concurrent_outcomes_preserve_both_increments(client, repo, monkeypatch):
    # Force both old read/modify/write requests to read the same snapshot. An
    # atomic delta update no longer needs this read, and cannot lose the increment.
    barrier = Barrier(2)
    get_run = repo.get_run

    def snapshot(run_id):
        run = get_run(run_id)
        barrier.wait(timeout=5)
        return run

    with monkeypatch.context() as patch:
        patch.setattr(repo, "get_run", snapshot)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post("/runs/run-1/outcome", json={
                "tool_name": "search", "ok": False, "tokens": 7, "cost_cents": 3,
            }), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    run = repo.get_run("run-1")
    assert (run.tokens_used, run.cost_cents, run.tool_calls, run.consecutive_errors) == (
        14, 6, 2, 2,
    )
    assert len(repo.recent_events("run-1", 100)) == 2


def test_failed_outcome_audit_insert_rolls_back_counters(client, repo):
    before = repo.get_run("run-1")
    with sqlite3.connect(repo._path) as db:
        db.execute("""CREATE TRIGGER reject_outcome BEFORE INSERT ON events
                      WHEN NEW.kind = 'outcome'
                      BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        client.post("/runs/run-1/outcome", json={
            "tool_name": "search", "ok": False, "tokens": 7,
        })
    assert repo.get_run("run-1") == before
    assert repo.recent_events("run-1", 100) == []


def test_a_stale_escalation_cannot_revive_a_terminated_run(repo):
    stale = repo.get_run("run-1")
    repo.record_state_change(*machine.complete(stale, now=T0))
    with pytest.raises(QuarantineError):
        repo.record_state_change(*machine.escalate(
            stale, cause="judge", actor="system", now=T0,
        ))
    assert repo.get_run("run-1").state is RunState.TERMINATED
    assert len(repo.transitions("run-1")) == 1


def test_a_stale_snapshot_is_rejected_even_after_a_same_time_state_cycle(repo):
    stale = repo.get_run("run-1")
    repo.record_state_change(*machine.escalate(
        stale, cause="loop", actor="system", now=T0,
    ))
    repo.record_state_change(*machine.auto_recover(repo.get_run("run-1"), now=T0))
    with pytest.raises(QuarantineError):
        repo.record_state_change(*machine.escalate(
            stale, cause="judge", actor="system", now=T0,
        ))
    assert repo.get_run("run-1").state is RunState.HEALTHY
    assert len(repo.transitions("run-1")) == 2


@pytest.mark.parametrize("severity", [Severity.CLEAR, Severity.CONCERN])
def test_a_judge_finishing_after_termination_never_authorizes_or_revives(repo, severity):
    class DelayedJudge:
        def evaluate(self, run, trajectory):
            repo.record_state_change(*machine.complete(repo.get_run(run.id), now=T0))
            return Verdict(severity, "result arrived after operator action")

    gate = Gate(repo, DelayedJudge(), FakeClock(), Settings())
    with pytest.raises(RunTerminated):
        gate.decide("run-1", ToolCall("issue_payment", "d1"))
    assert repo.get_run("run-1").state is RunState.TERMINATED
    assert len(repo.transitions("run-1")) == 1
    assert not any(e.payload.get("decision") == "allow"
                   for e in repo.recent_events("run-1", 100))


def test_heartbeat_cannot_overwrite_concurrent_usage(client, repo, monkeypatch):
    stale = repo.get_run("run-1")
    repo.save_run(replace(stale, tokens_used=21, tool_calls=3))
    with monkeypatch.context() as patch:
        # Model the snapshot an earlier heartbeat read before the outcome landed.
        # The new route never reads or persists this obsolete counter snapshot.
        patch.setattr(repo, "get_run", lambda _: stale)
        assert client.post("/runs/run-1/heartbeat").status_code == 200
    assert repo.get_run("run-1").tokens_used == 21
    assert repo.get_run("run-1").tool_calls == 3


def test_clean_high_risk_call_still_passes(repo):
    gate = Gate(repo, FakeJudge(), FakeClock(), Settings())
    assert gate.decide("run-1", ToolCall("issue_payment", "d1")) is Decision.ALLOW
    assert [e.kind for e in repo.recent_events("run-1", 100)] == [
        EventKind.PROPOSED, EventKind.JUDGE, EventKind.DECISION,
    ]
