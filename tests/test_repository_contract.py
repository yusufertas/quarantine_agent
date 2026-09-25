"""One contract, two implementations: the fake most tests trust, and the store
that ships.

`tests/conftest.py`'s `InMemoryRepository` stands in for SQLite in every gate,
API, reaper and detector test. The reading guide says it "is known to diverge
from the real SQLite store in at least two places" and does not say which. A
fake that drifts is the quiet way a suite goes green against a store that does
not exist, so this file runs the behaviours production code actually depends on
against BOTH implementations, and then names the divergences precisely -- pinned
on both sides, so a change to either is a visible change rather than a silent
one.

The second half is SQLite alone: properties the fake cannot have (a file, a
schema, foreign keys, a journal mode) and that nothing else asserted.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from conftest import InMemoryRepository, T0, make_event, make_run, make_transition
from quarantine.domain import machine
from quarantine.domain.states import EventKind, RunState
from quarantine.errors import ConcurrentStateChange, UnknownRun
from quarantine.sqlite_store import SqliteRepository


@pytest.fixture(params=["in_memory", "sqlite"])
def repo(request, tmp_path):
    if request.param == "in_memory":
        return InMemoryRepository()
    store = SqliteRepository(str(tmp_path / "contract.db"))
    store.initialize()
    return store


class TestRuns:
    def test_a_created_run_reads_back_equal(self, repo):
        run = make_run(tokens_used=3, cost_cents=4, tool_calls=5, consecutive_errors=1)
        repo.create_run(run)
        assert repo.get_run("run-1") == run

    def test_an_unknown_run_is_unknown_run_never_none(self, repo):
        with pytest.raises(UnknownRun):
            repo.get_run("nope")

    def test_save_run_persists_counters_and_heartbeat(self, repo):
        repo.create_run(make_run())
        later = T0 + timedelta(seconds=30)
        repo.save_run(make_run(tokens_used=7, tool_calls=2, last_heartbeat_at=later))
        loaded = repo.get_run("run-1")
        assert (loaded.tokens_used, loaded.tool_calls) == (7, 2)
        assert loaded.last_heartbeat_at == later

    def test_record_state_change_moves_state_and_appends_its_row(self, repo):
        repo.create_run(make_run())
        moved, transition = machine.escalate(
            repo.get_run("run-1"), cause="loop", actor="system", now=T0, detail="3x"
        )
        committed = repo.record_state_change(moved, transition)
        loaded = repo.get_run("run-1")
        assert loaded == committed
        assert loaded.state is RunState.DEGRADED
        assert loaded.state_since == T0
        assert loaded.state_revision > moved.state_revision
        assert repo.transitions("run-1") == [transition]

    def test_a_stale_snapshot_cannot_write_state(self, repo):
        """The contract that closes the audit's lost-containment race, on both
        implementations: whoever wrote last wins, and the loser is told."""
        repo.create_run(make_run())
        stale = repo.get_run("run-1")
        repo.record_state_change(*machine.escalate(stale, cause="loop", actor="system", now=T0))
        with pytest.raises(ConcurrentStateChange):
            repo.record_state_change(
                *machine.escalate(stale, cause="judge", actor="system", now=T0)
            )
        assert repo.get_run("run-1").state is RunState.DEGRADED
        assert [t.cause for t in repo.transitions("run-1")] == ["loop"]

    def test_transitions_come_back_in_the_order_they_were_written(self, repo):
        repo.create_run(make_run())
        run = repo.get_run("run-1")
        for cause in ("loop", "judge"):
            # The committed run carries the new revision; the machine's output
            # does not, and a second write from it is (correctly) refused.
            run = repo.record_state_change(
                *machine.escalate(run, cause=cause, actor="system", now=T0)
            )
        assert [t.cause for t in repo.transitions("run-1")] == ["loop", "judge"]

    def test_transitions_are_scoped_to_their_run(self, repo):
        repo.create_run(make_run(run_id="a"))
        repo.create_run(make_run(run_id="b"))
        repo.append_transition(make_transition(run_id="a"))
        assert [t.run_id for t in repo.transitions("a")] == ["a"]
        assert list(repo.transitions("b")) == []

    def test_list_runs_filters_by_state(self, repo):
        repo.create_run(make_run(run_id="h", state=RunState.HEALTHY))
        repo.create_run(make_run(run_id="f", state=RunState.FROZEN))
        assert {r.id for r in repo.list_runs()} == {"h", "f"}
        assert [r.id for r in repo.list_runs(RunState.FROZEN)] == ["f"]
        assert list(repo.list_runs(RunState.TERMINATED)) == []


class TestStaleHeartbeats:
    """The reaper's only query. 'Stale' means strictly older than the cutoff."""

    def test_strictly_older_than_the_cutoff(self, repo):
        repo.create_run(make_run(run_id="old", last_heartbeat_at=T0 - timedelta(seconds=1)))
        repo.create_run(make_run(run_id="exact", last_heartbeat_at=T0))
        repo.create_run(make_run(run_id="fresh", last_heartbeat_at=T0 + timedelta(seconds=1)))
        assert [r.id for r in repo.runs_with_stale_heartbeat(T0)] == ["old"]

    @pytest.mark.parametrize(
        "state", [RunState.HEALTHY, RunState.DEGRADED, RunState.FROZEN]
    )
    def test_every_live_state_is_a_candidate(self, repo, state):
        """FROZEN is returned too -- the reaper, not the store, decides that a
        frozen run's silence is expected. Filtering here would hide that
        decision in a query."""
        repo.create_run(make_run(state=state, last_heartbeat_at=T0))
        assert [r.id for r in repo.runs_with_stale_heartbeat(T0 + timedelta(hours=1))] == ["run-1"]

    def test_terminated_runs_are_never_candidates(self, repo):
        repo.create_run(make_run(state=RunState.TERMINATED, last_heartbeat_at=T0))
        assert list(repo.runs_with_stale_heartbeat(T0 + timedelta(days=1))) == []


class TestEvents:
    def test_recent_events_is_the_tail_in_seq_order(self, repo):
        repo.create_run(make_run())
        for seq in range(1, 6):
            repo.append_event(make_event(seq=seq, args_digest=f"d{seq}"))
        tail = repo.recent_events("run-1", 3)
        assert [e.seq for e in tail] == [3, 4, 5]
        assert [e.args_digest for e in tail] == ["d3", "d4", "d5"]

    def test_a_limit_beyond_the_log_returns_everything(self, repo):
        repo.create_run(make_run())
        repo.append_event(make_event(seq=1))
        assert len(repo.recent_events("run-1", 100)) == 1

    def test_next_seq_is_one_past_the_last_append(self, repo):
        repo.create_run(make_run())
        assert repo.next_seq("run-1") == 1
        for _ in range(3):
            repo.append_event(make_event(seq=repo.next_seq("run-1")))
        assert repo.next_seq("run-1") == 4

    def test_events_are_scoped_to_their_run(self, repo):
        repo.create_run(make_run(run_id="a"))
        repo.create_run(make_run(run_id="b"))
        repo.append_event(make_event(run_id="a", seq=1))
        assert list(repo.recent_events("b", 10)) == []
        assert repo.next_seq("b") == 1

    def test_payload_and_nullable_columns_round_trip(self, repo):
        repo.create_run(make_run())
        repo.append_event(
            make_event(seq=1, kind=EventKind.SYSTEM, tool_name=None, args_digest=None, note="x")
        )
        event = repo.recent_events("run-1", 1)[0]
        assert event.kind is EventKind.SYSTEM
        assert event.tool_name is None and event.args_digest is None
        assert event.payload == {"note": "x"}


class TestKnownDivergences:
    """Where the fake and the store deliberately differ. Both sides pinned: if
    either moves, this is where it shows.

    1. `save_run` writes `state` in the fake (the tests' raw state-setter) and
       CANNOT in SQLite (the machine-only-writer invariant, enforced by the
       storage layer).
    2. `save_run` of an unknown run is created by the fake and rejected by
       SQLite. The fake's leniency is what lets `store.save_run(make_run(...))`
       stand in for registration in the API tests.

    `event.seq` used to be a third: honoured by the fake, allocated by SQLite.
    Both allocate now, and `test_seq_is_allocated_by_both` keeps it that way.
    """

    def test_save_run_and_state(self, tmp_path):
        fake = InMemoryRepository()
        fake.create_run(make_run(state=RunState.DEGRADED))
        fake.save_run(make_run(state=RunState.HEALTHY))
        assert fake.get_run("run-1").state is RunState.HEALTHY

        real = SqliteRepository(str(tmp_path / "d.db"))
        real.initialize()
        real.create_run(make_run(state=RunState.DEGRADED))
        real.save_run(make_run(state=RunState.HEALTHY))
        assert real.get_run("run-1").state is RunState.DEGRADED

    def test_save_run_of_an_unknown_run(self, tmp_path):
        fake = InMemoryRepository()
        fake.save_run(make_run(run_id="ghost"))
        assert fake.get_run("ghost").id == "ghost"

        real = SqliteRepository(str(tmp_path / "d.db"))
        real.initialize()
        with pytest.raises(UnknownRun):
            real.save_run(make_run(run_id="ghost"))

    def test_seq_is_allocated_by_both(self, repo):
        """`event.seq` is advisory everywhere: the caller's 7 becomes 1, and the
        allocated value is what `append_event` returns."""
        repo.create_run(make_run())
        assert repo.append_event(make_event(seq=7)) == 1
        assert [e.seq for e in repo.recent_events("run-1", 10)] == [1]


@pytest.fixture
def sqlite_repo(tmp_path):
    store = SqliteRepository(str(tmp_path / "invariants.db"))
    store.initialize()
    return store


class TestSqliteInvariants:
    def test_initialize_is_idempotent_and_keeps_data(self, sqlite_repo):
        """`build()` runs it on every boot; a second boot must not wipe the store."""
        sqlite_repo.create_run(make_run())
        sqlite_repo.initialize()
        assert sqlite_repo.get_run("run-1").id == "run-1"

    def test_the_journal_is_wal(self, sqlite_repo):
        """The module's first line promises it; concurrent readers during a
        write depend on it, and nothing else checks the PRAGMA took effect."""
        with sqlite3.connect(sqlite_repo._path) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_orphan_rows_are_rejected_not_stored(self, sqlite_repo):
        """`PRAGMA foreign_keys=ON` is per connection and easy to lose in a
        refactor of `_connect`. Without it an event or audit row for a run that
        does not exist is stored silently, and `IntegrityError` -- which the
        boundary deliberately does not translate into an outage -- never fires."""
        with pytest.raises(sqlite3.IntegrityError):
            sqlite_repo.append_event(make_event(run_id="ghost", seq=1))
        with pytest.raises(sqlite3.IntegrityError):
            sqlite_repo.append_transition(make_transition(run_id="ghost"))
        assert list(sqlite_repo.recent_events("ghost", 10)) == []
        assert list(sqlite_repo.transitions("ghost")) == []

    def test_a_state_change_for_a_ghost_run_leaves_no_audit_row(self, sqlite_repo):
        """The transaction in `record_state_change`, from the other side: the
        run is read under the write lock first, so a ghost is refused as
        `UnknownRun` before any row is touched -- and no transition row exists
        for a run that was never there."""
        moved, transition = machine.escalate(
            make_run(run_id="ghost"), cause="loop", actor="system", now=T0
        )
        with pytest.raises(UnknownRun):
            sqlite_repo.record_state_change(moved, transition)
        assert list(sqlite_repo.transitions("ghost")) == []

    def test_duplicate_registration_is_an_integrity_error_not_an_outage(self, sqlite_repo):
        """Spec §9: a constraint violation stays itself. Dressing it as
        `StoreUnavailable` would make every worker fail OPEN on low-risk calls
        over a caller bug."""
        sqlite_repo.create_run(make_run())
        with pytest.raises(sqlite3.IntegrityError):
            sqlite_repo.create_run(make_run())

    def test_timestamps_keep_their_timezone(self, sqlite_repo):
        """Every datetime is stored as ISO text and compared as text by the
        reaper's query; an aware value must come back aware and equal."""
        sqlite_repo.create_run(make_run())
        loaded = sqlite_repo.get_run("run-1")
        assert loaded.created_at == T0
        assert loaded.created_at.tzinfo is not None
        assert loaded.created_at.utcoffset() == timedelta(0)
