"""Storage under concurrent writers, against a real SQLite file.

Two races that a single-threaded suite cannot see, and that the production path
already has: FastAPI runs these sync handlers in a threadpool, and `main.py`
hands background judging to a `ThreadPoolExecutor` whose JUDGE append lands at an
arbitrary point in the worker's call stream. Both get worse the moment
`Reaper.sweep()` is scheduled.

The first race is the one that makes the system *lie*: a lost update that erases
a containment while its audit row survives, leaving a run whose transitions say
DEGRADED and whose state says HEALTHY. An operator surface reporting containment
that is no longer in force is worse than one reporting none.

No fakes here. The in-memory repository cannot exhibit either race, which is
exactly why these must run against the implementation that ships.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

import pytest

from conftest import T0, make_event, make_run
from quarantine.domain import machine
from quarantine.domain.states import RunState
from quarantine.sqlite_store import SqliteRepository


@pytest.fixture
def repo(tmp_path):
    store = SqliteRepository(str(tmp_path / "concurrent.db"))
    store.initialize()
    return store


class TestStateSurvivesConcurrentCounterWrites:
    def test_an_escalation_is_not_erased_by_an_in_flight_outcome(self, repo):
        """The lost update, reproduced in the small.

        A worker reads the run, an escalation lands, and the worker writes its
        counters back from the stale snapshot. The escalation must survive: it is
        the containment decision, and the transition row recording it does.
        """
        repo.create_run(make_run(state=RunState.HEALTHY))
        stale = repo.get_run("run-1")          # the snapshot, taken first

        escalated, transition = machine.escalate(
            stale, cause="loop", actor="system", now=T0
        )
        repo.record_state_change(escalated, transition)

        # ... and only now does the outcome report land, from the stale read.
        repo.save_run(replace(stale, tokens_used=120, tool_calls=1))

        loaded = repo.get_run("run-1")
        assert loaded.state is RunState.DEGRADED, (
            "a counter write erased a containment; the transition log still "
            "records it, so the run now contradicts its own audit trail"
        )
        assert loaded.tokens_used == 120, "the counter update was lost instead"

    def test_state_and_counters_survive_concurrent_writers(self, repo):
        """The same thing under real threads, with the escalation in the middle."""
        repo.create_run(make_run(state=RunState.HEALTHY))
        snapshot = repo.get_run("run-1")
        writes = 60

        def heartbeat(i: int) -> None:
            repo.save_run(replace(snapshot, tool_calls=i))

        def escalate() -> None:
            escalated, transition = machine.escalate(
                snapshot, cause="call_rate", actor="system", now=T0
            )
            repo.record_state_change(escalated, transition)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(heartbeat, i) for i in range(writes // 2)]
            futures.append(pool.submit(escalate))
            futures += [
                pool.submit(heartbeat, i) for i in range(writes // 2, writes)
            ]
            for future in as_completed(futures):
                future.result()          # re-raise anything a thread swallowed

        assert repo.get_run("run-1").state is RunState.DEGRADED
        assert len(repo.transitions("run-1")) == 1

    def test_a_terminated_run_is_not_revived_by_a_late_heartbeat(self, repo):
        """The end of the ladder is the case that matters most: a straggling
        heartbeat must not put a TERMINATED run back on it."""
        repo.create_run(make_run(state=RunState.HEALTHY))
        stale = repo.get_run("run-1")
        done, transition = machine.complete(stale, now=T0)
        repo.record_state_change(done, transition)

        repo.save_run(replace(stale, last_heartbeat_at=T0))

        assert repo.get_run("run-1").state is RunState.TERMINATED


class TestConcurrentEventAppends:
    def test_concurrent_appends_all_land(self, repo):
        """`seq` is allocated inside the INSERT, so no two appends collide.

        Allocating on one connection and inserting on another loses rows to the
        `(run_id, seq)` primary key: the reviewer drove 200 concurrent appends
        that way and got 42 rows and 158 UNIQUE-constraint failures.
        """
        repo.create_run(make_run())
        appends = 200

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [
                pool.submit(repo.append_event, make_event(seq=0, args_digest=f"d{i}"))
                for i in range(appends)
            ]
            errors = [f.exception() for f in as_completed(futures)]

        assert [e for e in errors if e is not None] == []
        events = repo.recent_events("run-1", appends * 2)
        assert len(events) == appends

    def test_sequence_numbers_stay_unique_and_contiguous(self, repo):
        """The trajectory an operator reads has to be a sequence, not a bag."""
        repo.create_run(make_run())
        appends = 100

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(
                pool.map(
                    repo.append_event,
                    [make_event(seq=0, args_digest=f"d{i}") for i in range(appends)],
                )
            )

        seqs = [e.seq for e in repo.recent_events("run-1", appends * 2)]
        assert seqs == list(range(1, appends + 1))

    def test_appends_are_scoped_per_run(self, repo):
        """Allocation reads the high-water mark for one run, not the table."""
        repo.create_run(make_run(run_id="a"))
        repo.create_run(make_run(run_id="b"))
        for _ in range(3):
            repo.append_event(make_event(run_id="a", seq=0))
        repo.append_event(make_event(run_id="b", seq=0))
        assert [e.seq for e in repo.recent_events("b", 10)] == [1]


class TestStateChangeIsAtomic:
    def test_a_failed_transition_insert_leaves_the_state_alone(self, repo):
        """Spec §4 invariant 4: no state change without its audit row.

        Forced here by pointing the transition at a run that does not exist, so
        the foreign key rejects the second statement of the pair. The first must
        roll back with it -- otherwise a crash between the two writes produces a
        contained run with nothing on the record explaining why.
        """
        repo.create_run(make_run(state=RunState.HEALTHY))
        escalated, transition = machine.escalate(
            repo.get_run("run-1"), cause="loop", actor="system", now=T0
        )
        orphaned = replace(transition, run_id="no-such-run")

        with pytest.raises(Exception):
            repo.record_state_change(escalated, orphaned)

        assert repo.get_run("run-1").state is RunState.HEALTHY
        assert repo.transitions("run-1") == []
