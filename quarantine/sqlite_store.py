"""SQLite (WAL) implementation of Repository.

Chosen for a single-box internal tool at tens of concurrent runs; it removes a
service from the deployment. It sits behind the Repository protocol so Postgres is a
swap rather than a rewrite, which is why this is a spec decision and not an ADR.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import datetime

from .domain.models import Event, Run, Transition
from .domain.states import EventKind, RunState
from .errors import StoreUnavailable, UnknownRun

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS runs (
    id                 TEXT PRIMARY KEY,
    agent_name         TEXT NOT NULL,
    state              TEXT NOT NULL,
    state_since        TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    last_heartbeat_at  TEXT NOT NULL,
    budget_tokens      INTEGER NOT NULL,
    budget_cost_cents  INTEGER NOT NULL,
    deadline_at        TEXT NOT NULL,
    tokens_used        INTEGER NOT NULL DEFAULT 0,
    cost_cents         INTEGER NOT NULL DEFAULT 0,
    tool_calls         INTEGER NOT NULL DEFAULT 0,
    consecutive_errors INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    run_id      TEXT NOT NULL REFERENCES runs(id),
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    tool_name   TEXT,
    args_digest TEXT,
    payload     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS transitions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES runs(id),
    from_state TEXT NOT NULL,
    to_state   TEXT NOT NULL,
    cause      TEXT NOT NULL,
    actor      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
CREATE INDEX IF NOT EXISTS idx_runs_heartbeat ON runs(last_heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_transitions_run ON transitions(run_id, created_at);
"""


class SqliteRepository:
    def __init__(self, path: str) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """One connection, with every sqlite3 failure translated at the boundary.

        `StoreUnavailable` is the §9 row: the control plane cannot decide without
        state, and must say so as a 503 rather than leaking a driver exception
        into a 500. The translation lives here, at the edge, so nothing above
        this module has to know what database is underneath it.

        Note what is NOT translated. `UnknownRun` is raised outside these blocks,
        because a missing row is a legitimate 404 and not an outage. Nor is
        `IntegrityError`: a constraint violation is a bug in the caller, and
        dressing it as an outage would make every worker fail *open* on low-risk
        calls for something that is not an outage at all.
        """
        try:
            connection = self._connect()
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"cannot open the run store: {exc}") from exc
        try:
            with closing(connection):
                yield connection
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as exc:
            raise StoreUnavailable(str(exc)) from exc

    def initialize(self) -> None:
        with self._session() as connection:
            connection.executescript(SCHEMA)

    def create_run(self, run: Run) -> None:
        with self._session() as connection:
            connection.execute(
                """INSERT INTO runs (
                    id, agent_name, state, state_since, created_at, last_heartbeat_at,
                    budget_tokens, budget_cost_cents, deadline_at,
                    tokens_used, cost_cents, tool_calls, consecutive_errors
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run.id, run.agent_name, run.state.value,
                    run.state_since.isoformat(), run.created_at.isoformat(),
                    run.last_heartbeat_at.isoformat(),
                    run.budget_tokens, run.budget_cost_cents, run.deadline_at.isoformat(),
                    run.tokens_used, run.cost_cents, run.tool_calls,
                    run.consecutive_errors,
                ),
            )

    def save_run(self, run: Run) -> None:
        """Counters and heartbeat. `state` and `state_since` are NOT in this UPDATE.

        That omission is the point, not an oversight. Callers reach here holding a
        snapshot read moments earlier; an `INSERT OR REPLACE` of the whole row would
        write that stale `state` back and silently erase an escalation that landed in
        between -- leaving a `transitions` row saying DEGRADED and a run saying
        HEALTHY, which is the operator surface reporting containment that no longer
        exists. `record_state_change` is the only path that touches `state`, which
        makes `domain/machine.py` the only writer by construction rather than by
        convention.
        """
        with self._session() as connection:
            cursor = connection.execute(
                """UPDATE runs SET
                       last_heartbeat_at = ?,
                       tokens_used = ?,
                       cost_cents = ?,
                       tool_calls = ?,
                       consecutive_errors = ?
                   WHERE id = ?""",
                (
                    run.last_heartbeat_at.isoformat(),
                    run.tokens_used, run.cost_cents, run.tool_calls,
                    run.consecutive_errors, run.id,
                ),
            )
            matched = cursor.rowcount
        if matched == 0:
            # An update that matched nothing is a lost write, not a no-op.
            raise UnknownRun(run.id)

    def record_state_change(self, run: Run, transition: Transition) -> None:
        """The only writer of `runs.state`, and it writes the audit row with it.

        `BEGIN IMMEDIATE` takes the write lock up front so the two statements
        commit or roll back together: spec §4 invariant 4 forbids a state change
        with no transition row, and two autocommit statements cannot promise that
        across a crash. Taking the lock immediately (rather than deferring to the
        first write) also keeps two concurrent escalations from interleaving.
        """
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE runs SET state = ?, state_since = ? WHERE id = ?",
                    (run.state.value, run.state_since.isoformat(), run.id),
                )
                connection.execute(
                    """INSERT INTO transitions
                       (run_id, from_state, to_state, cause, actor, detail, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        transition.run_id, transition.from_state.value,
                        transition.to_state.value, transition.cause, transition.actor,
                        transition.detail, transition.created_at.isoformat(),
                    ),
                )
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def get_run(self, run_id: str) -> Run:
        with self._session() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownRun(run_id)
        return _row_to_run(row)

    def list_runs(self, state: RunState | None = None) -> Sequence[Run]:
        query, params = "SELECT * FROM runs", ()
        if state is not None:
            query, params = query + " WHERE state = ?", (state.value,)
        with self._session() as connection:
            rows = connection.execute(query + " ORDER BY created_at", params).fetchall()
        return [_row_to_run(row) for row in rows]

    def append_event(self, event: Event) -> None:
        """Append one trajectory row, allocating `seq` inside the INSERT.

        `SELECT COALESCE(MAX(seq),0)+1` as part of the insert makes allocation and
        insertion a single statement, and therefore a single implicit transaction.
        Reading `next_seq()` on one connection and inserting on another leaves a
        window in which two appends pick the same number and one of them dies on
        the `(run_id, seq)` primary key -- not hypothetical, since background
        judging appends from a thread pool while the worker's own calls are in
        flight. `event.seq` is consequently advisory and is ignored here.
        """
        with self._session() as connection:
            connection.execute(
                """INSERT INTO events
                   (run_id, seq, kind, created_at, tool_name, args_digest, payload)
                   SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ?
                     FROM events WHERE run_id = ?""",
                (
                    event.run_id, event.kind.value,
                    event.created_at.isoformat(), event.tool_name, event.args_digest,
                    json.dumps(event.payload, default=str),
                    event.run_id,
                ),
            )

    def recent_events(self, run_id: str, limit: int) -> Sequence[Event]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [_row_to_event(row) for row in reversed(rows)]

    def next_seq(self, run_id: str) -> int:
        with self._session() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS top FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["top"]) + 1

    def append_transition(self, transition: Transition) -> None:
        with self._session() as connection:
            connection.execute(
                """INSERT INTO transitions
                   (run_id, from_state, to_state, cause, actor, detail, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    transition.run_id, transition.from_state.value,
                    transition.to_state.value, transition.cause, transition.actor,
                    transition.detail, transition.created_at.isoformat(),
                ),
            )

    def transitions(self, run_id: str) -> Sequence[Transition]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM transitions WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [_row_to_transition(row) for row in rows]

    def runs_with_stale_heartbeat(self, cutoff: datetime) -> Sequence[Run]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE last_heartbeat_at < ? AND state != ?",
                (cutoff.isoformat(), RunState.TERMINATED.value),
            ).fetchall()
        return [_row_to_run(row) for row in rows]


def _row_to_run(row) -> Run:
    return Run(
        id=row["id"],
        agent_name=row["agent_name"],
        state=RunState(row["state"]),
        state_since=datetime.fromisoformat(row["state_since"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        last_heartbeat_at=datetime.fromisoformat(row["last_heartbeat_at"]),
        budget_tokens=row["budget_tokens"],
        budget_cost_cents=row["budget_cost_cents"],
        deadline_at=datetime.fromisoformat(row["deadline_at"]),
        tokens_used=row["tokens_used"],
        cost_cents=row["cost_cents"],
        tool_calls=row["tool_calls"],
        consecutive_errors=row["consecutive_errors"],
    )


def _row_to_event(row) -> Event:
    return Event(
        run_id=row["run_id"],
        seq=row["seq"],
        kind=EventKind(row["kind"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        tool_name=row["tool_name"],
        args_digest=row["args_digest"],
        payload=json.loads(row["payload"]),
    )


def _row_to_transition(row) -> Transition:
    return Transition(
        run_id=row["run_id"],
        from_state=RunState(row["from_state"]),
        to_state=RunState(row["to_state"]),
        cause=row["cause"],
        actor=row["actor"],
        detail=row["detail"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
