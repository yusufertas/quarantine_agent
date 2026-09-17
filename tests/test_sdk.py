"""The worker-side client library.

This is a convenience over the protocol, not a trust boundary: nothing here
is relied on for a safety property. What it must get right is the client-side
half of the fail-open/fail-closed tiering, because that is the behaviour that
applies exactly when the control plane cannot enforce anything.
"""

from __future__ import annotations

import pytest

from quarantine.errors import RunTerminated, UnknownRun
from quarantine.sdk import (
    QuarantineClient,
    RunFrozen,
    ToolRefused,
    TransportError,
)

LOW = "search"
HIGH = "issue_payment"


class FakeTransport:
    def __init__(self, decision: str = "allow", unreachable: bool = False):
        self.decision = decision
        self.unreachable = unreachable
        self.posts: list[tuple[str, dict]] = []

    def post(self, path: str, body: dict) -> dict:
        if self.unreachable:
            raise ConnectionError("control plane unreachable")
        self.posts.append((path, body))
        return {"decision": self.decision, "ok": True}


def client_with(**kwargs) -> tuple[QuarantineClient, FakeTransport]:
    transport = FakeTransport(**kwargs)
    return QuarantineClient(run_id="run-1", transport=transport), transport


class TestGuard:
    def test_allowed_call_runs(self):
        client, _ = client_with(decision="allow")
        assert client.guard(LOW, "d1", lambda: "result") == "result"

    def test_denied_call_raises_a_refusal_the_agent_can_react_to(self):
        """A DENY is a refusal, not an opaque failure -- a DEGRADED agent that
        treats it as fatal gets no benefit from the state existing."""
        client, _ = client_with(decision="deny")
        with pytest.raises(ToolRefused):
            client.guard(HIGH, "d1", lambda: "result")

    def test_frozen_run_raises_a_distinct_error(self):
        client, _ = client_with(decision="freeze")
        with pytest.raises(RunFrozen):
            client.guard(LOW, "d1", lambda: "result")

    def test_a_refused_call_never_executes_the_action(self):
        client, _ = client_with(decision="deny")
        calls = []
        with pytest.raises(ToolRefused):
            client.guard(HIGH, "d1", lambda: calls.append("ran"))
        assert calls == []

    def test_reports_the_outcome_after_a_successful_call(self):
        client, transport = client_with(decision="allow")
        client.guard(LOW, "d1", lambda: "ok")
        assert any(path.endswith("/outcome") for path, _ in transport.posts)

    def test_reports_a_failure_when_the_action_raises(self):
        client, transport = client_with(decision="allow")
        with pytest.raises(ValueError):
            client.guard(LOW, "d1", lambda: (_ for _ in ()).throw(ValueError("boom")))
        outcomes = [body for path, body in transport.posts if path.endswith("/outcome")]
        assert outcomes and outcomes[-1]["ok"] is False


class TestUnreachableControlPlane:
    def test_high_risk_call_fails_closed(self):
        """No supervision, no destructive action."""
        client, _ = client_with(unreachable=True)
        with pytest.raises(ToolRefused):
            client.guard(HIGH, "d1", lambda: "result")

    def test_low_risk_call_fails_open(self):
        client, _ = client_with(unreachable=True)
        assert client.guard(LOW, "d1", lambda: "result") == "result"

    def test_unclassified_tool_fails_closed(self):
        """Same default as the server: unknown means HIGH."""
        client, _ = client_with(unreachable=True)
        with pytest.raises(ToolRefused):
            client.guard("brand_new_tool", "d1", lambda: "result")

    def test_an_unreachable_heartbeat_does_not_crash_the_agent(self):
        client, _ = client_with(unreachable=True)
        client.heartbeat()          # must not raise


class ErrorTransport:
    """A transport honouring the documented contract: TransportError on non-2xx.

    The README's `raise_for_status()` transport used to raise the HTTP library's
    own exception instead, which is why a 503 crashed the agent rather than
    degrading it -- the client cannot tier on an exception type it does not know.
    """

    def __init__(self, status: int, detail: str = "boom"):
        self.status = status
        self.detail = detail
        self.posts: list[tuple[str, dict]] = []

    def post(self, path: str, body: dict) -> dict:
        self.posts.append((path, body))
        raise TransportError(self.status, self.detail)


class TestStoreOutageIsTieredLikeAnOutage:
    """Spec §9 puts a store outage in the same row as an unreachable control
    plane: a reachable server that cannot reach its own state protects a worker
    no better than a silent one."""

    def test_high_risk_call_fails_closed_on_503(self):
        client = QuarantineClient("run-1", ErrorTransport(503))
        with pytest.raises(ToolRefused):
            client.guard(HIGH, "d1", lambda: "result")

    def test_low_risk_call_fails_open_on_503(self):
        client = QuarantineClient("run-1", ErrorTransport(503))
        assert client.guard(LOW, "d1", lambda: "result") == "result"

    def test_unclassified_tool_fails_closed_on_503(self):
        """Unclassified is HIGH here exactly as it is on the server: a worker
        must not gain permissions by the control plane losing its database."""
        client = QuarantineClient("run-1", ErrorTransport(503))
        with pytest.raises(ToolRefused):
            client.guard("brand_new_tool", "d1", lambda: "result")

    @pytest.mark.parametrize("status", [502, 504])
    def test_other_gateway_statuses_tier_the_same_way(self, status):
        client = QuarantineClient("run-1", ErrorTransport(status))
        assert client.guard(LOW, "d1", lambda: "result") == "result"

    def test_a_503_never_escapes_guard(self):
        """The defect this covers: `guard()` raising a raw transport exception
        crashes the agent, which is neither fail-closed nor fail-open."""
        client = QuarantineClient("run-1", ErrorTransport(503))
        try:
            client.guard(LOW, "d1", lambda: "result")
        except TransportError:            # pragma: no cover - the regression
            pytest.fail("a 503 escaped guard() instead of being tiered")

    def test_telemetry_during_an_outage_does_not_crash_the_agent(self):
        client = QuarantineClient("run-1", ErrorTransport(503))
        client.heartbeat()
        client.report(LOW, ok=True)
        client.complete()                 # none of these may raise


class TestTypedErrors:
    """404 and 409 are answers, not outages. A worker developer should catch the
    package's own errors rather than pattern-match somebody's HTTP exception."""

    def test_unknown_run_is_surfaced_as_unknown_run(self):
        client = QuarantineClient("run-1", ErrorTransport(404, "no such run"))
        with pytest.raises(UnknownRun):
            client.gate(LOW, "d1")

    def test_terminated_run_is_surfaced_as_run_terminated(self):
        client = QuarantineClient("run-1", ErrorTransport(409, "terminated"))
        with pytest.raises(RunTerminated):
            client.gate(LOW, "d1")

    def test_a_404_is_not_tiered_into_a_decision(self):
        """Fail-open on a 404 would let a worker with a bad run id keep acting
        unsupervised forever, which is the opposite of what an unknown run means."""
        client = QuarantineClient("run-1", ErrorTransport(404))
        with pytest.raises(UnknownRun):
            client.guard(LOW, "d1", lambda: "result")

    def test_an_unexpected_status_propagates_as_itself(self):
        """No silent swallowing: an unclassified status is a bug to be seen."""
        client = QuarantineClient("run-1", ErrorTransport(418))
        with pytest.raises(TransportError):
            client.gate(LOW, "d1")

    def test_the_typed_error_names_the_run(self):
        client = QuarantineClient("run-7", ErrorTransport(404))
        with pytest.raises(UnknownRun, match="run-7"):
            client.gate(LOW, "d1")
