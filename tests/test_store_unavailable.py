"""Spec §9's "storage unavailable" row, on both halves.

| Storage unavailable | Control plane cannot decide; returns `503`. Workers apply
  the same tiering client-side: `HIGH` blocked, `LOW` proceeds. |

`StoreUnavailable` existed as a declaration and was never raised, caught or
handled: a store failure surfaced as an unhandled exception and a 500, and the
worker -- whose transport raises straight through `guard()` on a 5xx -- crashed
the agent. Neither fail-closed nor fail-open, which is the one outcome the
tiering exists to rule out.

`tests/test_failure_modes.py` asserts that the gate *propagates* a store failure,
which is true and insufficient: propagating to an unhandled 500 is what it did.
These tests cover what happens either side of that propagation.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from conftest import FakeJudge, make_event, make_run
from quarantine.api import create_app
from quarantine.errors import StoreUnavailable, UnknownRun
from quarantine.sdk import QuarantineClient, ToolRefused, TransportError
from quarantine.sqlite_store import SqliteRepository


class TestRepositoryRaisesStoreUnavailable:
    """Translated at the boundary, so nothing above knows what a `sqlite3` is."""

    @pytest.fixture
    def broken(self, tmp_path):
        """A path that cannot be opened as a database: a directory."""
        return SqliteRepository(str(tmp_path))

    @pytest.fixture
    def corrupt(self, tmp_path):
        path = tmp_path / "corrupt.db"
        path.write_bytes(b"this is not a SQLite file" * 100)
        return SqliteRepository(str(path))

    def test_reading_a_run_raises_store_unavailable(self, corrupt):
        with pytest.raises(StoreUnavailable):
            corrupt.get_run("run-1")

    def test_listing_runs_raises_store_unavailable(self, corrupt):
        with pytest.raises(StoreUnavailable):
            corrupt.list_runs()

    def test_writing_raises_store_unavailable(self, corrupt):
        with pytest.raises(StoreUnavailable):
            corrupt.create_run(make_run())

    def test_an_unopenable_path_raises_store_unavailable(self, broken):
        with pytest.raises(StoreUnavailable):
            broken.get_run("run-1")

    def test_a_missing_run_is_still_unknown_run_not_an_outage(self, tmp_path):
        """The line that must not blur: a 404 is an answer, a 503 is an outage."""
        repo = SqliteRepository(str(tmp_path / "fine.db"))
        repo.initialize()
        with pytest.raises(UnknownRun):
            repo.get_run("nope")

    def test_an_integrity_violation_is_not_dressed_up_as_an_outage(self, tmp_path):
        """A constraint violation is a caller bug. Reporting it as 503 would make
        every worker fail *open* on low-risk calls for something that is not an
        outage at all."""
        repo = SqliteRepository(str(tmp_path / "fk.db"))
        repo.initialize()
        with pytest.raises(sqlite3.IntegrityError):
            repo.append_event(make_event(run_id="no-such-run", seq=0))


class TestApiReturns503:
    @pytest.fixture
    def client(self, clock, settings):
        class BrokenStore:
            def get_run(self, run_id):
                raise StoreUnavailable("disk went away")

            def create_run(self, run):
                raise StoreUnavailable("disk went away")

            def list_runs(self, state=None):
                raise StoreUnavailable("disk went away")

        return TestClient(
            create_app(
                store=BrokenStore(), judge=FakeJudge(), clock=clock, settings=settings
            )
        )

    def test_a_gate_call_during_a_store_outage_is_503(self, client):
        """Not 500: the worker tiers on this status, and cannot tier on a bug."""
        response = client.post(
            "/runs/run-1/gate", json={"tool_name": "search", "args_digest": "d1"}
        )
        assert response.status_code == 503

    def test_registration_during_a_store_outage_is_503(self, client):
        response = client.post(
            "/runs",
            json={
                "agent_name": "a",
                "budget_tokens": 1,
                "budget_cost_cents": 1,
                "deadline_seconds": 1,
            },
        )
        assert response.status_code == 503

    def test_the_operator_surface_is_503_too(self, client):
        response = client.get(
            "/runs",
            headers={"X-Operator-Token": "test-token", "X-Operator-Id": "op-1"},
        )
        assert response.status_code == 503

    def test_the_body_says_what_happened(self, client):
        """An operator paged at 3am reads this string."""
        response = client.post(
            "/runs/run-1/gate", json={"tool_name": "search", "args_digest": "d1"}
        )
        assert "disk went away" in response.json()["detail"]


class TestWorkerSurvivesAStoreOutageEndToEnd:
    """Both halves joined: a real app whose store is down, a real SDK client.

    This is the test that would have caught the defect. Each half in isolation
    looked defensible -- the gate propagated the failure, the SDK tiered on
    `ConnectionError` -- and the gap sat exactly between them: a 5xx is a
    *reachable* server, so nothing tiered and the agent crashed.
    """

    @pytest.fixture
    def transport(self, clock, settings):
        class BrokenStore:
            def get_run(self, run_id):
                raise StoreUnavailable("disk went away")

        app = create_app(
            store=BrokenStore(), judge=FakeJudge(), clock=clock, settings=settings
        )
        http = TestClient(app)

        class Transport:
            """The shape the README documents, honouring the contract."""

            def post(self, path: str, body: dict) -> dict:
                response = http.post(path, json=body)
                if response.status_code >= 400:
                    raise TransportError(response.status_code, response.text)
                return response.json()

        return Transport()

    def test_a_low_risk_call_proceeds(self, transport):
        client = QuarantineClient(run_id="run-1", transport=transport)
        assert client.guard("search", "d1", lambda: "result") == "result"

    def test_a_high_risk_call_is_refused(self, transport):
        client = QuarantineClient(run_id="run-1", transport=transport)
        with pytest.raises(ToolRefused):
            client.guard("issue_payment", "d1", lambda: "paid")

    def test_the_agent_does_not_crash_on_a_transport_exception(self, transport):
        """The failure mode in one line: before this, the exception escaped."""
        client = QuarantineClient(run_id="run-1", transport=transport)
        client.guard("search", "d1", lambda: "result")
        client.heartbeat()
