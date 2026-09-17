"""The operator surface as a whole: all five human-only routes, and its defaults.

`test_api.py`'s `TestOperatorAuthorization` covers rejection on `release` and
`terminate` only. The three inspection routes are equally operator-only -- a
frozen run's trajectory is the most sensitive thing this system holds, since it
is a verbatim record of everything an agent was about to do -- and nothing
asserted they reject an unauthenticated caller. A `Depends` dropped from a `GET`
signature would have been a silent, untested regression.

This file also pins the two things that make the surface usable rather than
merely correct: an absent release body meaning the documented default, and an
error that says what went wrong.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import FakeJudge, T0, make_run
from quarantine.api import create_app
from quarantine.domain.states import RunState

OPERATOR = {"X-Operator-Token": "test-token", "X-Operator-Id": "operator-1"}
WRONG_TOKEN = {"X-Operator-Token": "guessed", "X-Operator-Id": "operator-1"}
NO_ID = {"X-Operator-Token": "test-token"}

# Every route that requires a human, as (method, path template). Adding an
# operator route without adding it here is the gap this file exists to close.
OPERATOR_ROUTES = [
    ("GET", "/runs"),
    ("GET", "/runs/{run_id}"),
    ("GET", "/runs/{run_id}/trajectory"),
    ("POST", "/runs/{run_id}/release"),
    ("POST", "/runs/{run_id}/terminate"),
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


@pytest.fixture
def frozen_run(client, store) -> str:
    run_id = register(client)
    store.save_run(make_run(run_id=run_id, state=RunState.FROZEN, now=T0))
    return run_id


def request(client: TestClient, method: str, path: str, run_id: str, headers: dict):
    url = path.format(run_id=run_id)
    if method == "GET":
        return client.get(url, headers=headers)
    return client.post(url, json={}, headers=headers)


class TestEveryOperatorRouteRequiresAHuman:
    """Not just the mutating two. Reading a frozen run's trajectory is an
    operator action, and inspection is where the sensitive material lives."""

    @pytest.mark.parametrize(("method", "path"), OPERATOR_ROUTES)
    def test_rejected_without_credentials(self, client, frozen_run, method, path):
        response = request(client, method, path, frozen_run, headers={})
        assert response.status_code in (401, 403), (
            f"{method} {path} answered {response.status_code} with no credentials"
        )

    @pytest.mark.parametrize(("method", "path"), OPERATOR_ROUTES)
    def test_rejected_with_a_wrong_token(self, client, frozen_run, method, path):
        response = request(client, method, path, frozen_run, headers=WRONG_TOKEN)
        assert response.status_code in (401, 403)

    @pytest.mark.parametrize(("method", "path"), OPERATOR_ROUTES)
    def test_rejected_without_an_operator_id(self, client, frozen_run, method, path):
        """A token identifies the tool, not the person. The transition records a
        named human or the request does not happen (spec §4, invariant 5)."""
        response = request(client, method, path, frozen_run, headers=NO_ID)
        assert response.status_code in (401, 403)

    @pytest.mark.parametrize(("method", "path"), OPERATOR_ROUTES)
    def test_accepted_with_credentials(self, client, frozen_run, method, path):
        """The other half: the rejection tests above would also pass if the
        routes were simply broken."""
        response = request(client, method, path, frozen_run, headers=OPERATOR)
        assert response.status_code == 200, response.text


class TestReleaseDefaults:
    def test_release_with_no_body_at_all_defaults_to_degraded(
        self, client, store, frozen_run
    ):
        """`POST /release` with an empty request, as `curl -X POST` sends it.

        A required body made the documented default reachable only by remembering
        to send `{}` -- an operator releasing a run by hand got a 422 and had to
        guess why. A default you have to opt into is not a default.
        """
        response = client.post(f"/runs/{frozen_run}/release", headers=OPERATOR)
        assert response.status_code == 200, response.text
        assert store.get_run(frozen_run).state is RunState.DEGRADED

    def test_the_safe_default_is_not_healthy(self, client, store, frozen_run):
        """Release keeps a run declawed until it has proven itself (ADR-0002)."""
        client.post(f"/runs/{frozen_run}/release", headers=OPERATOR)
        assert store.get_run(frozen_run).state is not RunState.HEALTHY

    def test_an_explicit_target_still_wins(self, client, store, frozen_run):
        client.post(
            f"/runs/{frozen_run}/release",
            json={"target": "healthy"},
            headers=OPERATOR,
        )
        assert store.get_run(frozen_run).state is RunState.HEALTHY


class TestErrorBodiesAreReadable:
    def test_a_terminated_run_says_so(self, client):
        """`{"detail": "<uuid>"} is not an error message -- the worker developer
        reading it learns only the id they already sent."""
        run_id = register(client)
        client.post(f"/runs/{run_id}/complete")
        response = client.post(
            f"/runs/{run_id}/gate", json={"tool_name": "search", "args_digest": "d1"}
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail != run_id
        assert "terminated" in detail.lower()


class TestRunSerialization:
    def test_the_operator_payload_carries_the_limits_not_just_the_counters(
        self, client, frozen_run
    ):
        """An operator asking "why was this frozen" needs `tokens_used` next to
        `budget_tokens`, and a `wall_clock` cause is unreadable without the
        deadline. The limits travel with the counters or the answer needs a
        second lookup."""
        payload = client.get(f"/runs/{frozen_run}", headers=OPERATOR).json()
        for field in (
            "budget_tokens",
            "budget_cost_cents",
            "deadline_at",
            "tokens_used",
            "cost_cents",
            "tool_calls",
            "consecutive_errors",
            "state_since",
        ):
            assert field in payload, f"{field} missing from the operator payload"
