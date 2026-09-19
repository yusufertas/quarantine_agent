"""Independent adversarial review using temporary SQLite, not an in-memory store.

Hooks force exact interleavings without sleeps, network access, or real judges.
Cadence is deliberately outside this review's scope.
"""

from datetime import timedelta
import sqlite3

from fastapi.testclient import TestClient
import pytest

from conftest import FakeClock, FakeJudge, T0, make_run
from quarantine.api import create_app
from quarantine.config import Settings
from quarantine.domain import machine
from quarantine.domain.models import Event, ToolCall, Verdict
from quarantine.domain.states import Decision, EventKind, RunState, Severity
from quarantine.errors import ConcurrentStateChange, RunTerminated
from quarantine.gate import Gate
from quarantine.reaper import Reaper
from quarantine.sqlite_store import SqliteRepository


@pytest.fixture
def repo(tmp_path):
    store = SqliteRepository(str(tmp_path / "concurrency-review.db"))
    store.initialize()
    return store


def outcome(run_id="run-1", now=T0):
    return Event(
        run_id=run_id, seq=0, kind=EventKind.OUTCOME, created_at=now,
        tool_name="search", payload={"ok": False, "tokens": 7, "cost_cents": 3},
    )


def approval(run_id="run-1"):
    return Event(
        run_id=run_id, seq=0, kind=EventKind.DECISION, created_at=T0,
        tool_name="issue_payment", args_digest="review",
        payload={"decision": Decision.ALLOW.value},
    )


def cycle(repo, run_id="run-1"):
    repo.record_state_change(*machine.escalate(
        repo.get_run(run_id), cause="loop", actor="system", now=T0,
    ))
    return repo.record_state_change(*machine.auto_recover(repo.get_run(run_id), now=T0))


@pytest.mark.parametrize("state", [RunState.HEALTHY, RunState.DEGRADED])
@pytest.mark.parametrize("refresh", ["heartbeat", "outcome"])
@pytest.mark.parametrize("boundary", ["selection", "commit"])
def test_reaper_revalidates_liveness_before_transition(
    repo, monkeypatch, state, refresh, boundary,
):
    repo.create_run(make_run(state=state))
    now = T0 + Settings().heartbeat_timeout + timedelta(seconds=1)
    observed = []

    def make_live():
        before = repo.get_run("run-1")
        if refresh == "heartbeat":
            repo.touch_heartbeat("run-1", now)
        else:
            repo.record_outcome(outcome(now=now))
        after = repo.get_run("run-1")
        assert after.last_heartbeat_at == now
        assert after.state_revision == before.state_revision
        observed.append(refresh)

    if boundary == "selection":
        select = repo.runs_with_stale_heartbeat

        def selected_then_live(cutoff):
            selected = select(cutoff)
            assert [run.id for run in selected] == ["run-1"]
            make_live()
            return selected

        monkeypatch.setattr(repo, "runs_with_stale_heartbeat", selected_then_live)
    else:
        commit = repo.record_state_change

        def live_before_commit(run, transition, *args, **kwargs):
            make_live()
            return commit(run, transition, *args, **kwargs)

        monkeypatch.setattr(repo, "record_state_change", live_before_commit)

    escalated = Reaper(repo, FakeClock(now), Settings()).sweep()
    assert observed == [refresh], "the competing liveness update must actually run"
    assert escalated == 0, "a committed heartbeat/outcome invalidates the timeout"
    assert repo.get_run("run-1").state is state
    assert repo.transitions("run-1") == []


@pytest.mark.parametrize("state", [RunState.HEALTHY, RunState.DEGRADED])
def test_reaper_positive_control_still_escalates_a_silent_run(repo, state):
    repo.create_run(make_run(state=state))
    now = T0 + Settings().heartbeat_timeout + timedelta(seconds=1)
    assert Reaper(repo, FakeClock(now), Settings()).sweep() == 1
    assert repo.get_run("run-1").state is machine.next_rung(state)
    assert [t.cause for t in repo.transitions("run-1")] == ["heartbeat_timeout"]


@pytest.mark.parametrize("route,state", [
    ("release", RunState.FROZEN), ("terminate", RunState.HEALTHY),
])
def test_operator_race_returns_409_without_overwriting_winner(
    repo, monkeypatch, route, state,
):
    repo.create_run(make_run(state=state))
    commit = repo.record_state_change
    intercepted = []

    def competing_termination(run, transition, *args, **kwargs):
        intercepted.append(transition.actor)
        commit(*machine.terminate(repo.get_run(run.id), actor="human:winner", now=T0))
        return commit(run, transition, *args, **kwargs)

    monkeypatch.setattr(repo, "record_state_change", competing_termination)
    app = create_app(repo, FakeJudge(), FakeClock(), Settings(operator_token="test-only"))
    with TestClient(app) as client:
        response = client.post(
            f"/runs/run-1/{route}", json={},
            headers={"X-Operator-Token": "test-only", "X-Operator-Id": "loser"},
        )
    assert intercepted == ["human:loser"]
    assert response.status_code == 409, response.text
    assert repo.get_run("run-1").state is RunState.TERMINATED
    assert [t.actor for t in repo.transitions("run-1")] == ["human:winner"]


def test_operator_release_rejects_same_time_frozen_aba(repo, monkeypatch):
    repo.create_run(make_run(state=RunState.FROZEN))
    commit = repo.record_state_change
    intercepted = []

    def competing_release_and_refreeze(run, transition, *args, **kwargs):
        released = commit(*machine.release(
            repo.get_run(run.id), RunState.DEGRADED, actor="human:winner", now=T0,
        ))
        commit(*machine.escalate(released, cause="judge", actor="system", now=T0))
        intercepted.append(repo.get_run(run.id).state_revision)
        return commit(run, transition, *args, **kwargs)

    monkeypatch.setattr(repo, "record_state_change", competing_release_and_refreeze)
    app = create_app(repo, FakeJudge(), FakeClock(), Settings(operator_token="test-only"))
    with TestClient(app) as client:
        response = client.post(
            "/runs/run-1/release", json={"target": "healthy"},
            headers={"X-Operator-Token": "test-only", "X-Operator-Id": "loser"},
        )
    assert intercepted == [2]
    assert response.status_code == 409, response.text
    assert repo.get_run("run-1").state is RunState.FROZEN
    assert len(repo.transitions("run-1")) == 2


def test_decision_rejects_same_time_aba_and_records_actual_answer(repo):
    repo.create_run(make_run())
    stale = repo.get_run("run-1")
    current = cycle(repo)
    assert (current.state, current.state_since) == (stale.state, stale.state_since)
    assert current.state_revision != stale.state_revision
    assert repo.record_decision(approval(), stale.state_revision) is Decision.DENY
    event = repo.recent_events("run-1", 1)[0]
    assert event.payload["decision"] == "deny"
    assert event.payload["state_revision"] == current.state_revision
    assert repo.record_decision(approval(), current.state_revision) is Decision.ALLOW


def test_clear_judge_after_same_time_aba_is_denied(repo):
    repo.create_run(make_run())

    class CyclingJudge:
        def evaluate(self, run, trajectory):
            cycle(repo, run.id)
            return Verdict(Severity.CLEAR, "fake result predates a state cycle")

    gate = Gate(repo, CyclingJudge(), FakeClock(), Settings())
    assert gate.decide("run-1", ToolCall("issue_payment", "review")) is Decision.DENY
    assert repo.get_run("run-1").state is RunState.HEALTHY
    assert repo.recent_events("run-1", 1)[0].payload["decision"] == "deny"


@pytest.mark.parametrize("state", [RunState.FROZEN, RunState.TERMINATED])
def test_decision_reads_current_containment_before_appending(repo, state):
    repo.create_run(make_run())
    stale = repo.get_run("run-1")
    if state is RunState.FROZEN:
        for _ in range(2):
            repo.record_state_change(*machine.escalate(
                repo.get_run("run-1"), cause="judge", actor="system", now=T0,
            ))
        assert repo.record_decision(approval(), stale.state_revision) is Decision.FREEZE
        assert repo.recent_events("run-1", 1)[0].payload["decision"] == "freeze"
    else:
        repo.record_state_change(*machine.complete(stale, now=T0))
        with pytest.raises(RunTerminated):
            repo.record_decision(approval(), stale.state_revision)
        assert repo.recent_events("run-1", 10) == []


def test_decision_validation_and_append_share_the_write_lock(repo, monkeypatch):
    repo.create_run(make_run())
    append = repo._append_event
    observed = []
    # Positive control: this independent writer can acquire the fixture's lock.
    with sqlite3.connect(repo._path, timeout=0, isolation_level=None) as other:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")

    def inspect_lock(connection, event):
        if event.kind is EventKind.DECISION:
            with sqlite3.connect(repo._path, timeout=0, isolation_level=None) as other:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("BEGIN IMMEDIATE")
            observed.append(connection.in_transaction)
        return append(connection, event)

    monkeypatch.setattr(repo, "_append_event", inspect_lock)
    assert repo.record_decision(approval(), 0) is Decision.ALLOW
    assert observed == [True]
    done = repo.record_state_change(*machine.complete(repo.get_run("run-1"), now=T0))
    assert done.state is RunState.TERMINATED


def test_state_change_returns_committed_revision_and_current_counters(repo):
    repo.create_run(make_run())
    old = repo.get_run("run-1")
    repo.record_outcome(outcome())
    committed = repo.record_state_change(*machine.escalate(
        old, cause="judge", actor="system", now=T0,
    ))
    assert committed == repo.get_run("run-1")
    assert committed.state_revision > old.state_revision
    assert (committed.tokens_used, committed.tool_calls) == (7, 1)
    recovered = repo.record_state_change(*machine.auto_recover(committed, now=T0))
    assert recovered.state is RunState.HEALTHY


def test_late_telemetry_cannot_move_heartbeat_backwards(repo):
    repo.create_run(make_run())
    latest = T0 + timedelta(minutes=5)
    repo.touch_heartbeat("run-1", latest)
    repo.record_outcome(outcome(now=T0 + timedelta(seconds=1)))
    repo.touch_heartbeat("run-1", T0 + timedelta(seconds=2))
    run = repo.get_run("run-1")
    assert run.last_heartbeat_at == latest
    assert (run.tokens_used, run.tool_calls, run.consecutive_errors) == (7, 1, 1)


# Frozen table layout from aab2c5b. No production SCHEMA import: the test must
# still exercise an older database if the current schema changes later.
LEGACY_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE runs (
    id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, state TEXT NOT NULL,
    state_since TEXT NOT NULL, created_at TEXT NOT NULL,
    last_heartbeat_at TEXT NOT NULL, budget_tokens INTEGER NOT NULL,
    budget_cost_cents INTEGER NOT NULL, deadline_at TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0, cost_cents INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0, consecutive_errors INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE events (
    run_id TEXT NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL,
    kind TEXT NOT NULL, created_at TEXT NOT NULL, tool_name TEXT,
    args_digest TEXT, payload TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (run_id, seq)
);
CREATE TABLE transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
    from_state TEXT NOT NULL, to_state TEXT NOT NULL, cause TEXT NOT NULL,
    actor TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX idx_runs_state ON runs(state);
CREATE INDEX idx_runs_heartbeat ON runs(last_heartbeat_at);
CREATE INDEX idx_transitions_run ON transitions(run_id, created_at);
"""


def test_existing_schema_preserves_history_and_hydrates_all_revision_readers(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_SCHEMA)
        for run_id, transition_id in (("legacy", 7), ("other", 900)):
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                run_id, "old-agent", "degraded", T0.isoformat(), T0.isoformat(),
                T0.isoformat(), 100_000, 1_000, (T0 + timedelta(hours=1)).isoformat(),
                9, 4, 2, 1,
            ))
            db.execute("INSERT INTO transitions VALUES (?,?,?,?,?,?,?,?)", (
                transition_id, run_id, "healthy", "degraded", "loop", "system",
                "historical audit", T0.isoformat(),
            ))
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (
            "legacy", 1, "proposed", T0.isoformat(), "search", "old", '{}',
        ))
        original = {table: db.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ("runs", "events", "transitions")}

    repo = SqliteRepository(path)
    repo.initialize()
    repo.initialize()
    with sqlite3.connect(path) as db:
        for table, rows in original.items():
            assert db.execute(f"SELECT * FROM {table}").fetchall() == rows
        assert "state_revision" not in {
            row[1] for row in db.execute("PRAGMA table_info(runs)")
        }
        assert [row[2] for row in db.execute(
            "PRAGMA index_info(idx_transitions_revision)"
        )] == ["run_id", "id"]

    expected = {"legacy": 7, "other": 900}
    assert {r.id: r.state_revision for r in repo.list_runs()} == expected
    assert {r.id: r.state_revision for r in repo.list_runs(RunState.DEGRADED)} == expected
    assert {r.id: r.state_revision for r in repo.runs_with_stale_heartbeat(
        T0 + timedelta(seconds=1)
    )} == expected
    stale = repo.get_run("legacy")
    assert stale.state_revision == 7
    current = repo.record_state_change(*machine.auto_recover(stale, now=T0))
    assert current.state_revision > 900
    assert current.tokens_used == 9
    assert repo.get_run("other").state_revision == 900
    with pytest.raises(ConcurrentStateChange):
        repo.record_state_change(*machine.auto_recover(stale, now=T0))
    reopened = SqliteRepository(path)
    reopened.initialize()
    assert reopened.get_run("legacy") == current
    assert reopened.recent_events("legacy", 10)[0].args_digest == "old"
