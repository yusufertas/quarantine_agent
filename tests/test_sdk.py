"""The worker-side client library.

This is a convenience over the protocol, not a trust boundary: nothing here
is relied on for a safety property. What it must get right is the client-side
half of the fail-open/fail-closed tiering, because that is the behaviour that
applies exactly when the control plane cannot enforce anything.
"""

from __future__ import annotations

import pytest

from quarantine.sdk import QuarantineClient, RunFrozen, ToolRefused

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
