"""What `ClaudeJudge` sends, and the one failure it must not re-wrap.

`test_claude_judge.py` covers the verdict mapping and that a transport failure
becomes `JudgeUnavailable`. Left open: whether the configured timeout actually
reaches the client (a judge that ignores its timeout is a HIGH-risk gate that
blocks forever), what the prompt contains (the run's counters and the whole
trajectory, oldest first, in the order the model is told to expect), that the
structured-output schema is the one `evaluate` validates against, and that a
`JudgeUnavailable` raised by the client itself passes through with its message
intact rather than being wrapped in a second one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conftest import make_event, make_run
from quarantine.domain.states import EventKind, Severity
from quarantine.errors import JudgeUnavailable
from quarantine.judge import SYSTEM_PROMPT, ClaudeJudge, JudgeVerdictModel


class RecordingClient:
    """Records `with_options(...)` and `messages.parse(...)` arguments."""

    def __init__(self, parsed=None, raises=None) -> None:
        self.options: list[dict] = []
        self.calls: list[dict] = []
        self._parsed = parsed
        self._raises = raises
        self.messages = self

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return type("Response", (), {"parsed_output": self._parsed})()


def clear() -> JudgeVerdictModel:
    return JudgeVerdictModel(severity="clear", reason="on task")


class TestRequestShape:
    def test_the_configured_timeout_reaches_the_client_with_retries_off(self):
        """One attempt per verdict: with the SDK's default retries a 10 s
        timeout could hold a HIGH-risk gate for several multiples of it."""
        client = RecordingClient(parsed=clear())
        ClaudeJudge(client=client, timeout_seconds=3.5).evaluate(make_run(), ())
        assert client.options == [{"timeout": 3.5, "max_retries": 0}]

    def test_the_configured_model_reaches_the_client(self):
        client = RecordingClient(parsed=clear())
        ClaudeJudge(client=client, model="claude-sonnet-5").evaluate(make_run(), ())
        assert client.calls[0]["model"] == "claude-sonnet-5"

    def test_structured_output_uses_the_verdict_schema(self):
        client = RecordingClient(parsed=clear())
        ClaudeJudge(client=client).evaluate(make_run(), ())
        assert client.calls[0]["output_format"] is JudgeVerdictModel

    def test_the_system_prompt_names_every_severity_it_may_return(self):
        """`evaluate` validates `Severity(parsed.severity)`; a prompt that
        offered a fourth word would turn every such answer into an outage."""
        for severity in Severity:
            assert f"- {severity.value}:" in SYSTEM_PROMPT
        client = RecordingClient(parsed=clear())
        ClaudeJudge(client=client).evaluate(make_run(), ())
        assert client.calls[0]["system"] == SYSTEM_PROMPT

    def test_the_prompt_carries_the_counters_and_the_trajectory_oldest_first(self):
        client = RecordingClient(parsed=clear())
        run = make_run(tool_calls=7, consecutive_errors=2, run_id="run-9")
        trajectory = (
            make_event(seq=1, tool_name="search", args_digest="q"),
            make_event(seq=2, kind=EventKind.DECISION, tool_name="search", decision="allow"),
            make_event(seq=3, tool_name="delete_records", args_digest="all"),
        )
        ClaudeJudge(client=client).evaluate(run, trajectory)
        prompt = client.calls[0]["messages"][0]["content"]
        assert client.calls[0]["messages"][0]["role"] == "user"
        assert "Tool calls so far: 7" in prompt
        assert "Consecutive errors: 2" in prompt
        assert "test-agent" in prompt
        assert prompt.index("[1] proposed search") < prompt.index("[2] decision search")
        assert prompt.index("[2] decision search") < prompt.index("[3] proposed delete_records")
        assert '"decision": "allow"' in prompt

    def test_a_payload_that_json_cannot_encode_does_not_break_the_prompt(self):
        """Payloads are free-form dicts; a datetime in one must render, not
        raise -- a rendering crash here would present as a judge outage."""
        client = RecordingClient(parsed=clear())
        event = make_event(seq=1, kind=EventKind.SYSTEM, at=datetime(2026, 1, 1, tzinfo=UTC))
        ClaudeJudge(client=client).evaluate(make_run(), (event,))
        assert "2026-01-01" in client.calls[0]["messages"][0]["content"]

    def test_an_empty_trajectory_is_still_sent(self):
        client = RecordingClient(parsed=clear())
        verdict = ClaudeJudge(client=client).evaluate(make_run(), ())
        assert verdict.severity is Severity.CLEAR
        assert "Recent trajectory (oldest first):" in client.calls[0]["messages"][0]["content"]


class TestJudgeUnavailablePassesThrough:
    def test_raised_by_the_client_it_is_not_wrapped_again(self):
        """A transport that already speaks the package's language must not
        have its message buried under 'judge call failed: ...'."""
        client = RecordingClient(raises=JudgeUnavailable("rate limited upstream"))
        with pytest.raises(JudgeUnavailable) as excinfo:
            ClaudeJudge(client=client).evaluate(make_run(), ())
        assert str(excinfo.value) == "rate limited upstream"
        assert excinfo.value.__cause__ is None

    def test_any_other_failure_is_wrapped_with_its_cause(self):
        client = RecordingClient(raises=TimeoutError("10s"))
        with pytest.raises(JudgeUnavailable) as excinfo:
            ClaudeJudge(client=client).evaluate(make_run(), ())
        assert isinstance(excinfo.value.__cause__, TimeoutError)
        assert "10s" in str(excinfo.value)

    def test_a_missing_reason_is_an_outage_not_a_verdict(self):
        """`parsed_output` can be None when the model refuses the schema."""
        client = RecordingClient(parsed=None)
        with pytest.raises(JudgeUnavailable):
            ClaudeJudge(client=client).evaluate(make_run(), ())
