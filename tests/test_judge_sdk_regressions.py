"""Judge/SDK regressions using local fakes only: no model, network or sleeps."""

from inspect import Parameter, signature
from types import SimpleNamespace

import pytest

from conftest import make_event, make_run
from quarantine.domain.states import Decision, Severity
from quarantine.errors import JudgeUnavailable
from quarantine.judge import ClaudeJudge, JudgeVerdictModel
from quarantine.sdk import QuarantineClient, RunFrozen, ToolRefused


class RecordingJudgeClient:
    def __init__(self, failure=None):
        self.failure = failure
        self.options = []
        self.calls = []
        self.messages = self

    def with_options(self, **options):
        self.options.append(options)
        return self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(
            parsed_output=JudgeVerdictModel(severity="clear", reason="fake verdict")
        )


class RecordingTransport:
    def __init__(self, decision="allow", unreachable=False):
        self.decision = decision
        self.unreachable = unreachable
        self.posts = []

    def post(self, path, body):
        self.posts.append((path, body))
        if self.unreachable:
            raise ConnectionError("synthetic outage")
        return {"decision": self.decision, "ok": True}


@pytest.mark.parametrize("timeout", [0.25, 10.0])
def test_judge_explicitly_disables_transport_retries(timeout):
    client = RecordingJudgeClient()
    judge = ClaudeJudge(client=client, timeout_seconds=timeout)

    verdict = judge.evaluate(make_run(), [make_event()])

    assert verdict.severity is Severity.CLEAR
    assert client.options == [{"timeout": timeout, "max_retries": 0}]
    assert len(client.calls) == 1


def test_transport_timeout_remains_unavailable_not_clear():
    failure = TimeoutError("synthetic request timeout")
    client = RecordingJudgeClient(failure=failure)

    with pytest.raises(JudgeUnavailable) as exc:
        ClaudeJudge(client=client).evaluate(make_run(), [make_event()])

    assert exc.value.__cause__ is failure
    assert len(client.calls) == 1


def test_digest_distinguishes_otherwise_identical_judge_prompts():
    client = RecordingJudgeClient()
    judge = ClaudeJudge(client=client)
    preview = '{"to": "billing@example.test"}'
    digests = ["a" * 64, "b" * 64, "a" * 64]

    for digest in digests:
        verdict = judge.evaluate(
            make_run(),
            [make_event(args_digest=digest, args_preview=preview)],
        )
        assert verdict.severity is Severity.CLEAR

    prompts = [call["messages"][0]["content"] for call in client.calls]
    assert prompts[0] == prompts[2]
    assert prompts[0] != prompts[1]
    assert digests[0] in prompts[0] and digests[1] not in prompts[0]
    assert digests[1] in prompts[1] and digests[0] not in prompts[1]
    assert "billing@example.test" in prompts[0]


def test_event_without_digest_still_renders_and_returns_a_verdict():
    client = RecordingJudgeClient()
    verdict = ClaudeJudge(client=client).evaluate(
        make_run(), [make_event(args_digest=None)]
    )

    assert verdict.severity is Severity.CLEAR
    assert "proposed search" in client.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize("method", ["gate", "guard"])
def test_args_preview_is_optional_and_keyword_only(method):
    parameter = signature(getattr(QuarantineClient, method)).parameters["args_preview"]
    assert parameter.kind is Parameter.KEYWORD_ONLY
    assert parameter.default == ""


@pytest.mark.parametrize("method", ["gate", "guard"])
def test_sdk_forwards_preview_without_changing_digest_or_action(method):
    transport = RecordingTransport()
    client = QuarantineClient("run-1", transport)
    preview = '{"to": "billing@example.test"}'
    actions = []

    def action():
        actions.append("ran")
        return "result"

    if method == "gate":
        decision = client.gate("send_email", "digest", args_preview=preview)
        assert decision is Decision.ALLOW
        assert actions == []
    else:
        result = client.guard("send_email", "digest", action, args_preview=preview)
        assert result == "result"
        assert actions == ["ran"]
        assert transport.posts[1] == (
            "/runs/run-1/outcome",
            {"tool_name": "send_email", "ok": True, "tokens": 0, "cost_cents": 0},
        )

    assert transport.posts[0] == (
        "/runs/run-1/gate",
        {"tool_name": "send_email", "args_digest": "digest", "args_preview": preview},
    )
    assert len(transport.posts) == (1 if method == "gate" else 2)


def test_existing_positional_gate_and_guard_calls_still_work():
    transport = RecordingTransport()
    client = QuarantineClient("run-1", transport)

    assert client.gate("search", "d1") is Decision.ALLOW
    assert client.guard("search", "d2", lambda: "result") == "result"
    requests = [body for path, body in transport.posts if path.endswith("/gate")]
    assert [body["args_digest"] for body in requests] == ["d1", "d2"]
    assert all(body.get("args_preview", "") == "" for body in requests)


@pytest.mark.parametrize("decision, error", [("deny", ToolRefused), ("freeze", RunFrozen)])
def test_preview_does_not_bypass_a_refusal(decision, error):
    transport = RecordingTransport(decision=decision)
    client = QuarantineClient("run-1", transport)
    actions = []

    with pytest.raises(error):
        client.guard(
            "send_email", "digest", lambda: actions.append("ran"), args_preview="send"
        )

    assert actions == []
    assert len(transport.posts) == 1


@pytest.mark.parametrize("tool", ["search", "issue_payment"])
def test_preview_preserves_offline_risk_tiering(tool):
    client = QuarantineClient("run-1", RecordingTransport(unreachable=True))
    actions = []

    def action():
        actions.append("ran")

    if tool == "search":
        client.guard(tool, "digest", action, args_preview="preview")
        assert actions == ["ran"]
    else:
        with pytest.raises(ToolRefused):
            client.guard(tool, "digest", action, args_preview="preview")
        assert actions == []
