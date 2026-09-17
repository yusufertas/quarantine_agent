"""SQLite (WAL) implementation of Repository.

Chosen for a single-box internal tool at tens of concurrent runs; it removes a
service from the deployment. It sits behind the Repository protocol so Postgres is a
swap rather than a rewrite, which is why this is a spec decision and not an ADR.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from .domain.models import Event, Run, Transition
from .domain.states import RunState

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

    def initialize(self) -> None:
        raise NotImplementedError("SqliteRepository.initialize")

    def create_run(self, run: Run) -> None:
        raise NotImplementedError("SqliteRepository.create_run")

    def get_run(self, run_id: str) -> Run:
        raise NotImplementedError("SqliteRepository.get_run")

    def save_run(self, run: Run) -> None:
        raise NotImplementedError("SqliteRepository.save_run")

    def list_runs(self, state: RunState | None = None) -> Sequence[Run]:
        raise NotImplementedError("SqliteRepository.list_runs")

    def append_event(self, event: Event) -> None:
        raise NotImplementedError("SqliteRepository.append_event")

    def recent_events(self, run_id: str, limit: int) -> Sequence[Event]:
        raise NotImplementedError("SqliteRepository.recent_events")

    def next_seq(self, run_id: str) -> int:
        raise NotImplementedError("SqliteRepository.next_seq")

    def append_transition(self, transition: Transition) -> None:
        raise NotImplementedError("SqliteRepository.append_transition")

    def transitions(self, run_id: str) -> Sequence[Transition]:
        raise NotImplementedError("SqliteRepository.transitions")

    def runs_with_stale_heartbeat(self, cutoff: datetime) -> Sequence[Run]:
        raise NotImplementedError("SqliteRepository.runs_with_stale_heartbeat")
