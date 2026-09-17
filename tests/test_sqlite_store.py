"""SqliteRepository against a real file.

The same behaviours the in-memory fake provides, verified against SQLite --
because the fake is what every other test trusts, and a divergence between
them would mean the suite is green against a store that does not exist.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import T0, make_event, make_run, make_transition
from quarantine.domain import machine
from quarantine.domain.states import EventKind, RunState
from quarantine.errors import UnknownRun
from quarantine.sqlite_store import SqliteRepository


@pytest.fixture
def repo(tmp_path):
    store = SqliteRepository(str(tmp_path / "test.db"))
    store.initialize()
    return store


class TestRuns:
    def test_round_trips_a_run(self, repo):
        repo.create_run(make_run(tokens_used=42))
        loaded = repo.get_run("run-1")
        assert loaded.id == "run-1"
        assert loaded.state is RunState.HEALTHY
        assert loaded.tokens_used == 42
        assert loaded.deadline_at == T0 + timedelta(hours=1)

    def test_unknown_run_raises(self, repo):
        with pytest.raises(UnknownRun):
            repo.get_run("nope")

    def test_save_updates_counters_in_place(self, repo):
        repo.create_run(make_run())
        repo.save_run(make_run(tokens_used=7, consecutive_errors=2))
        loaded = repo.get_run("run-1")
        assert (loaded.tokens_used, loaded.consecutive_errors) == (7, 2)
        assert len(repo.list_runs()) == 1

    def test_save_cannot_write_state(self, repo):
        """The storage-level enforcement of the machine-only-writer invariant.

        Callers hold a snapshot; if `save_run` carried `state`, a counter update
        racing an escalation would write the pre-escalation state back and leave
        a run whose transition log says DEGRADED and whose state says HEALTHY.
        """
        repo.create_run(make_run(state=RunState.DEGRADED))
        repo.save_run(make_run(state=RunState.HEALTHY, tokens_used=7))
        loaded = repo.get_run("run-1")
        assert loaded.state is RunState.DEGRADED
        assert loaded.tokens_used == 7

    def test_save_of_an_unknown_run_is_not_silently_lost(self, repo):
        with pytest.raises(UnknownRun):
            repo.save_run(make_run(run_id="never-registered"))

    def test_record_state_change_writes_state_and_its_audit_row(self, repo):
        repo.create_run(make_run(state=RunState.HEALTHY))
        updated, transition = machine.escalate(
            repo.get_run("run-1"), cause="loop", actor="system", now=T0
        )
        repo.record_state_change(updated, transition)
        assert repo.get_run("run-1").state is RunState.DEGRADED
        assert [t.cause for t in repo.transitions("run-1")] == ["loop"]

    def test_record_state_change_leaves_counters_alone(self, repo):
        """It takes `state` from the machine's output and nothing else, so an
        escalation cannot clobber a concurrent outcome report."""
        repo.create_run(make_run(state=RunState.HEALTHY))
        repo.save_run(make_run(tokens_used=42, tool_calls=9))
        stale, transition = machine.escalate(
            make_run(state=RunState.HEALTHY), cause="loop", actor="system", now=T0
        )
        repo.record_state_change(stale, transition)
        loaded = repo.get_run("run-1")
        assert loaded.state is RunState.DEGRADED
        assert (loaded.tokens_used, loaded.tool_calls) == (42, 9)

    def test_list_filters_by_state(self, repo):
        repo.create_run(make_run(run_id="a", state=RunState.HEALTHY))
        repo.create_run(make_run(run_id="b", state=RunState.FROZEN))
        assert [r.id for r in repo.list_runs(RunState.FROZEN)] == ["b"]

    def test_stale_heartbeat_excludes_terminated_runs(self, repo):
        repo.create_run(make_run(run_id="live", last_heartbeat_at=T0))
        repo.create_run(
            make_run(run_id="done", state=RunState.TERMINATED, last_heartbeat_at=T0)
        )
        stale = repo.runs_with_stale_heartbeat(T0 + timedelta(minutes=5))
        assert [r.id for r in stale] == ["live"]


class TestEvents:
    def test_appends_and_reads_back_in_order(self, repo):
        repo.create_run(make_run())
        for seq in (1, 2, 3):
            repo.append_event(make_event(seq=seq, args_digest=f"d{seq}"))
        assert [e.seq for e in repo.recent_events("run-1", 10)] == [1, 2, 3]

    def test_recent_events_returns_the_tail(self, repo):
        repo.create_run(make_run())
        for seq in range(1, 6):
            repo.append_event(make_event(seq=seq))
        assert [e.seq for e in repo.recent_events("run-1", 2)] == [4, 5]

    def test_payload_survives_the_round_trip(self, repo):
        repo.create_run(make_run())
        repo.append_event(make_event(seq=1, kind=EventKind.JUDGE, severity="concern"))
        assert repo.recent_events("run-1", 1)[0].payload["severity"] == "concern"

    def test_next_seq_counts_from_one(self, repo):
        repo.create_run(make_run())
        assert repo.next_seq("run-1") == 1
        repo.append_event(make_event(seq=1))
        assert repo.next_seq("run-1") == 2


class TestTransitions:
    def test_appends_and_reads_back(self, repo):
        repo.create_run(make_run())
        repo.append_transition(make_transition(cause="loop"))
        recorded = repo.transitions("run-1")
        assert len(recorded) == 1
        assert recorded[0].cause == "loop"
        assert recorded[0].from_state is RunState.HEALTHY

    def test_scoped_to_one_run(self, repo):
        repo.create_run(make_run(run_id="a"))
        repo.create_run(make_run(run_id="b"))
        repo.append_transition(make_transition(run_id="a"))
        assert repo.transitions("b") == []
