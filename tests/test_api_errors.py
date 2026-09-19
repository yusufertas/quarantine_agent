"""What the HTTP surface says when it says no.

`api.py` registers five exception handlers. Two of them -- `IllegalTransition`
-> 409 and `AuthorizationRequired` -> 403 -- had never been reached by any test:
every operator test drove a permitted transition, so a handler returning 500 (or
a handler quietly dropped) would have gone unnoticed until an operator released
a run that was not frozen and read a stack trace.

The other half of the same question: which routes answer 404 for a run that does
not exist. All nine run-scoped routes must, worker and operator alike -- a
worker protocol route falling through to a permissive default is exactly what
spec §9's first row forbids.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import FakeJudge, T0, make_run
from quarantine.api import create_app
from quarantine.domain.states import RunState

OPERATOR = {"X-Operator-Token": "test-token", "X-Operator-Id": "operator-1"}

# Every route that takes a run id, as (method, path template, body or None).
RUN_SCOPED_ROUTES = [
    ("POST", "/runs/{run_id}/gate", {"tool_name": "search", "args_digest": "d1"}),
    ("POST", "/runs/{run_id}/outcome", {"tool_name": "search", "ok": True}),
    ("POST", "/runs/{run_id}/heartbeat", None),
    ("POST", "/runs/{run_id}/complete", None),
    ("GET", "/runs/{run_id}", None),
    ("GET", "/runs/{run_id}/trajectory", None),
    ("POST", "/runs/{run_id}/release", None),
    ("POST", "/runs/{run_id}/terminate", {}),
]


@pytest.fixture
def judge():
    return FakeJudge()


@pytest.fixture
def client(store, judge, clock, settings):
    return TestClient(create_app(store=store, judge=judge, clock=clock, settings=settings))


def register(client: TestClient) -> str:
    response = client.post(
        "/runs",
        json={
            "agent_name": "test-agent",
            "budget_tokens": 100_000,
            "budget_cost_cents": 1_000,
            "deadline_seconds": 3_600,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def in_state(client, store, state: RunState) -> str:
    run_id = register(client)
    store.save_run(make_run(run_id=run_id, state=state, now=T0))
    return run_id


class TestUnknownRunIsAlwaysA404:
    @pytest.mark.parametrize(("method", "path", "body"), RUN_SCOPED_ROUTES)
    def test_every_run_scoped_route(self, client, method, path, body):
        url = path.format(run_id="no-such-run")
        if method == "GET":
            response = client.get(url, headers=OPERATOR)
        else:
            response = client.post(url, json=body, headers=OPERATOR)
        assert response.status_code == 404, f"{method} {path} -> {response.status_code}"

    def test_the_body_names_the_run(self, client):
        response = client.post("/runs/ghost-7/heartbeat")
        assert "ghost-7" in response.json()["detail"]


class TestIllegalTransitionsAre409:
    """The transition table says no; the API must say so as a conflict, not as
    an internal error, and must leave the run and its audit trail untouched."""

    @pytest.mark.parametrize("state", [RunState.HEALTHY, RunState.DEGRADED])
    def test_releasing_a_run_that_is_not_frozen(self, client, store, state):
        run_id = in_state(client, store, state)
        response = client.post(f"/runs/{run_id}/release", headers=OPERATOR)
        assert response.status_code == 409, response.text
        assert store.get_run(run_id).state is state

    def test_releasing_a_terminated_run(self, client, store):
        run_id = in_state(client, store, RunState.TERMINATED)
        assert client.post(f"/runs/{run_id}/release", headers=OPERATOR).status_code == 409

    @pytest.mark.parametrize("target", ["frozen", "terminated"])
    def test_release_cannot_target_a_state_at_or_above_frozen(
        self, client, store, target
    ):
        """Release goes DOWN the ladder. 'Releasing' a run into TERMINATED is a
        termination without the termination verb's audit cause."""
        run_id = in_state(client, store, RunState.FROZEN)
        response = client.post(
            f"/runs/{run_id}/release", json={"target": target}, headers=OPERATOR
        )
        assert response.status_code == 409, response.text
        assert store.get_run(run_id).state is RunState.FROZEN

    def test_completing_a_frozen_run(self, client, store):
        """A contained run ends by human decision, not by its worker declaring
        success -- the machine says so and the API must relay it."""
        run_id = in_state(client, store, RunState.FROZEN)
        response = client.post(f"/runs/{run_id}/complete")
        assert response.status_code == 409
        assert store.get_run(run_id).state is RunState.FROZEN

    def test_completing_twice(self, client, store):
        run_id = register(client)
        assert client.post(f"/runs/{run_id}/complete").status_code == 200
        assert client.post(f"/runs/{run_id}/complete").status_code == 409

    def test_terminating_a_terminated_run(self, client, store):
        run_id = in_state(client, store, RunState.TERMINATED)
        response = client.post(f"/runs/{run_id}/terminate", json={}, headers=OPERATOR)
        assert response.status_code == 409

    def test_a_refused_transition_writes_no_audit_row(self, client, store):
        """Invariant 4 in the other direction: no transition row without a state
        change, just as no state change without a transition row."""
        run_id = in_state(client, store, RunState.HEALTHY)
        client.post(f"/runs/{run_id}/release", headers=OPERATOR)
        client.post(f"/runs/{run_id}/release", json={"target": "healthy"}, headers=OPERATOR)
        assert store.transitions(run_id) == []

    def test_the_body_explains_the_refusal(self, client, store):
        """An operator who typed the wrong verb reads this, not a status code."""
        run_id = in_state(client, store, RunState.HEALTHY)
        detail = client.post(f"/runs/{run_id}/release", headers=OPERATOR).json()["detail"]
        assert "healthy" in detail.lower()
        assert "release" in detail.lower()


class TestMalformedInputIs422:
    """Validation failures are the caller's bug and must be rejected before any
    storage is touched -- a half-validated request must not create a run."""

    def test_an_unknown_release_target(self, client, store):
        run_id = in_state(client, store, RunState.FROZEN)
        response = client.post(
            f"/runs/{run_id}/release", json={"target": "paroled"}, headers=OPERATOR
        )
        assert response.status_code == 422
        assert store.get_run(run_id).state is RunState.FROZEN

    def test_an_unknown_state_filter(self, client):
        assert client.get("/runs", params={"state": "sleepy"}, headers=OPERATOR).status_code == 422

    def test_a_gate_call_without_a_digest(self, client):
        run_id = register(client)
        response = client.post(f"/runs/{run_id}/gate", json={"tool_name": "search"})
        assert response.status_code == 422

    def test_registration_with_a_missing_field_creates_nothing(self, client, store):
        response = client.post("/runs", json={"agent_name": "half"})
        assert response.status_code == 422
        assert store.list_runs() == []

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "`terminate` declares its body without a default instance, so "
            "`curl -X POST .../terminate` -- the emergency verb, sent by hand -- "
            "answers 422 while `release` with no body answers 200. The release "
            "docstring's own argument applies: a default you must opt into is not "
            "a default. One-line fix: `body: TerminateRequest = TerminateRequest()`."
        ),
    )
    def test_terminate_with_no_body_at_all_is_accepted(self, client, store):
        run_id = in_state(client, store, RunState.FROZEN)
        response = client.post(f"/runs/{run_id}/terminate", headers=OPERATOR)
        assert response.status_code == 200, response.text
        assert store.get_run(run_id).state is RunState.TERMINATED


class TestOperatorIdentityIsRecordedVerbatim:
    def test_terminate_records_the_operator_id(self, client, store):
        run_id = in_state(client, store, RunState.DEGRADED)
        client.post(
            f"/runs/{run_id}/terminate",
            json={"detail": "runaway"},
            headers={"X-Operator-Token": "test-token", "X-Operator-Id": "alice"},
        )
        transition = store.transitions(run_id)[-1]
        assert transition.actor == "human:alice"
        assert transition.cause == "terminated"
        assert transition.detail == "runaway"
        assert transition.is_human

    def test_release_detail_travels_to_the_audit_row(self, client, store):
        run_id = in_state(client, store, RunState.FROZEN)
        client.post(
            f"/runs/{run_id}/release",
            json={"detail": "reviewed trajectory; false positive"},
            headers=OPERATOR,
        )
        assert store.transitions(run_id)[-1].detail == "reviewed trajectory; false positive"
