"""HTTP surface (spec §6).

Two audiences with different authorisation: worker protocol endpoints that any
registered worker calls, and operator endpoints that require a human. Releasing a
FROZEN run is the operation that must never become reachable without one.

Wired against an in-memory repository and a fake judge -- no database, no model.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import FakeJudge, T0, make_run
from quarantine.api import create_app
from quarantine.domain.states import RunState

OPERATOR = {"X-Operator-Token": "test-token", "X-Operator-Id": "operator-1"}
WRONG_TOKEN = {"X-Operator-Token": "guessed", "X-Operator-Id": "operator-1"}


@pytest.fixture
def judge():
    return FakeJudge()


@pytest.fixture
def client(store, judge, clock, settings):
    return TestClient(create_app(store=store, judge=judge, clock=clock, settings=settings))


def register(client, **overrides) -> str:
    body = {
        "agent_name": "test-agent",
        "budget_tokens": 100_000,
        "budget_cost_cents": 1_000,
        "deadline_seconds": 3_600,
    } | overrides
    response = client.post("/runs", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


class TestWorkerProtocol:
    def test_register_returns_a_run_id(self, client):
        assert register(client)

    def test_gate_returns_a_decision(self, client):
        run_id = register(client)
        response = client.post(
            f"/runs/{run_id}/gate",
            json={"tool_name": "search", "args_digest": "d1"},
        )
        assert response.status_code == 200
        assert response.json()["decision"] == "allow"

    def test_gate_denies_a_high_risk_call_from_a_degraded_run(self, client, store):
        run_id = register(client)
        store.save_run(make_run(run_id=run_id, state=RunState.DEGRADED, now=T0))
        response = client.post(
            f"/runs/{run_id}/gate",
            json={"tool_name": "issue_payment", "args_digest": "d1"},
        )
        assert response.status_code == 200
        assert response.json()["decision"] == "deny"

    def test_gate_freezes_every_call_from_a_frozen_run(self, client, store):
        run_id = register(client)
        store.save_run(make_run(run_id=run_id, state=RunState.FROZEN, now=T0))
        response = client.post(
            f"/runs/{run_id}/gate", json={"tool_name": "search", "args_digest": "d1"}
        )
        assert response.json()["decision"] == "freeze"

    def test_gate_on_unknown_run_is_404(self, client):
        response = client.post(
            "/runs/no-such-run/gate", json={"tool_name": "search", "args_digest": "d1"}
        )
        assert response.status_code == 404

    def test_gate_on_terminated_run_is_409(self, client):
        run_id = register(client)
        client.post(f"/runs/{run_id}/complete")
        response = client.post(
            f"/runs/{run_id}/gate", json={"tool_name": "search", "args_digest": "d1"}
        )
        assert response.status_code == 409

    def test_heartbeat_is_accepted(self, client):
        run_id = register(client)
        assert client.post(f"/runs/{run_id}/heartbeat").status_code == 200

    def test_outcome_updates_counters(self, client, store):
        run_id = register(client)
        client.post(
            f"/runs/{run_id}/outcome",
            json={"tool_name": "search", "ok": False, "tokens": 120, "cost_cents": 3},
        )
        run = store.get_run(run_id)
        assert run.consecutive_errors == 1
        assert run.tokens_used == 120

    def test_successful_outcome_resets_the_error_streak(self, client, store):
        run_id = register(client)
        for ok in (False, False, True):
            client.post(
                f"/runs/{run_id}/outcome", json={"tool_name": "search", "ok": ok}
            )
        assert store.get_run(run_id).consecutive_errors == 0

    def test_complete_terminates_the_run(self, client, store):
        run_id = register(client)
        assert client.post(f"/runs/{run_id}/complete").status_code == 200
        assert store.get_run(run_id).state is RunState.TERMINATED


class TestOperatorAuthorization:
    """A worker must never be able to release itself."""

    @pytest.fixture
    def frozen_run(self, client, store):
        run_id = register(client)
        store.save_run(make_run(run_id=run_id, state=RunState.FROZEN, now=T0))
        return run_id

    def test_release_without_credentials_is_rejected(self, client, frozen_run):
        response = client.post(f"/runs/{frozen_run}/release", json={})
        assert response.status_code in (401, 403)

    def test_release_with_a_wrong_token_is_rejected(self, client, frozen_run):
        response = client.post(
            f"/runs/{frozen_run}/release", json={}, headers=WRONG_TOKEN
        )
        assert response.status_code in (401, 403)

    def test_terminate_without_credentials_is_rejected(self, client, frozen_run):
        assert client.post(f"/runs/{frozen_run}/terminate", json={}).status_code in (401, 403)


class TestRelease:
    @pytest.fixture
    def frozen_run(self, client, store):
        run_id = register(client)
        store.save_run(make_run(run_id=run_id, state=RunState.FROZEN, now=T0))
        return run_id

    def test_release_defaults_to_degraded(self, client, store, frozen_run):
        """The safe default: keep the run declawed until it has proven itself."""
        response = client.post(f"/runs/{frozen_run}/release", json={}, headers=OPERATOR)
        assert response.status_code == 200
        assert store.get_run(frozen_run).state is RunState.DEGRADED

    def test_release_can_target_healthy_explicitly(self, client, store, frozen_run):
        client.post(
            f"/runs/{frozen_run}/release", json={"target": "healthy"}, headers=OPERATOR
        )
        assert store.get_run(frozen_run).state is RunState.HEALTHY

    def test_release_records_who_did_it(self, client, store, frozen_run):
        client.post(f"/runs/{frozen_run}/release", json={}, headers=OPERATOR)
        transition = store.transitions(frozen_run)[-1]
        assert transition.actor == "human:operator-1"
        assert transition.is_human


class TestInspection:
    def test_list_runs_filters_by_state(self, client, store):
        register(client)
        store.save_run(make_run(run_id="frozen-1", state=RunState.FROZEN))
        response = client.get("/runs", params={"state": "frozen"}, headers=OPERATOR)
        assert response.status_code == 200
        assert [r["id"] for r in response.json()] == ["frozen-1"]

    def test_run_detail_includes_transition_history(self, client, store):
        run_id = register(client)
        client.post(f"/runs/{run_id}/complete")
        response = client.get(f"/runs/{run_id}", headers=OPERATOR)
        assert response.status_code == 200
        assert response.json()["transitions"]

    def test_trajectory_returns_the_event_log(self, client):
        run_id = register(client)
        client.post(
            f"/runs/{run_id}/gate", json={"tool_name": "search", "args_digest": "d1"}
        )
        response = client.get(f"/runs/{run_id}/trajectory", headers=OPERATOR)
        assert response.status_code == 200
        assert len(response.json()) >= 2  # PROPOSED + DECISION
