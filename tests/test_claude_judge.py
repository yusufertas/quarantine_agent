"""The Claude-backed judge.

No test here reaches the network: the Anthropic client is a stub. What is
being tested is the contract the gate depends on -- that a verdict comes back
structured, and that a transport failure raises JudgeUnavailable rather than
degrading into a CLEAR verdict.
"""

from __future__ import annotations

import pytest

from conftest import T0, make_event, make_run
from quarantine.domain.states import Severity
from quarantine.errors import JudgeUnavailable
from quarantine.judge import ClaudeJudge, JudgeVerdictModel


class StubMessages:
    def __init__(self, parsed=None, raises=None):
        self.parsed = parsed
        self.raises = raises
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return type("Response", (), {"parsed_output": self.parsed})()


class StubClient:
    def __init__(self, messages):
        self.messages = messages

    def with_options(self, **kwargs):
        return self


def judge_with(parsed=None, raises=None) -> tuple[ClaudeJudge, StubMessages]:
    messages = StubMessages(parsed=parsed, raises=raises)
    return ClaudeJudge(client=StubClient(messages)), messages


TRAJECTORY = (make_event(seq=1), make_event(seq=2, tool_name="issue_payment"))


class TestVerdicts:
    @pytest.mark.parametrize(
        "severity", [Severity.CLEAR, Severity.CONCERN, Severity.SEVERE]
    )
    def test_maps_each_severity(self, severity):
        judge, _ = judge_with(
            JudgeVerdictModel(severity=severity.value, reason="because")
        )
        verdict = judge.evaluate(make_run(now=T0), TRAJECTORY)
        assert verdict.severity is severity
        assert verdict.reason == "because"

    def test_sends_the_trajectory_to_the_model(self):
        judge, messages = judge_with(
            JudgeVerdictModel(severity="clear", reason="on task")
        )
        judge.evaluate(make_run(now=T0), TRAJECTORY)
        prompt = str(messages.calls[0]["messages"])
        assert "issue_payment" in prompt

    def test_uses_the_configured_model(self):
        judge, messages = judge_with(
            JudgeVerdictModel(severity="clear", reason="on task")
        )
        judge.evaluate(make_run(now=T0), TRAJECTORY)
        assert messages.calls[0]["model"] == "claude-opus-5"


class TestTransportFailure:
    def test_transport_error_raises_judge_unavailable(self):
        """Never a CLEAR verdict on failure -- that would make an outage look
        like an approval."""
        judge, _ = judge_with(raises=RuntimeError("connection reset"))
        with pytest.raises(JudgeUnavailable):
            judge.evaluate(make_run(now=T0), TRAJECTORY)

    def test_unparseable_severity_raises_judge_unavailable(self):
        judge, _ = judge_with(JudgeVerdictModel(severity="fine", reason="?"))
        with pytest.raises(JudgeUnavailable):
            judge.evaluate(make_run(now=T0), TRAJECTORY)
