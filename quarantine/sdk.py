"""Worker-side client for the quarantine control plane.

Wraps tool execution: ask before, report after, heartbeat throughout.

This is a convenience, NOT a trust boundary (ADR-0001). Every safety property
must hold when a worker bypasses this module -- which is what the server's
heartbeat reaper is for. Never move a check in here that the server relies on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .domain import registry
from .domain.states import Decision, ToolRisk
from .errors import QuarantineError, RunTerminated, UnknownRun

# Statuses that mean "the control plane cannot decide right now" rather than "the
# control plane says no". Spec §9 puts a storage outage in the same row as an
# unreachable control plane, so the client-side tiering must treat them alike: a
# reachable server that cannot reach its own store protects a worker no better
# than an unreachable one.
UNAVAILABLE_STATUSES = frozenset({502, 503, 504})


class TransportError(QuarantineError):
    """The control plane answered, but with an error status.

    Exists so the SDK can tier on *which* error. A transport that lets an
    `httpx.HTTPStatusError` (or any other library's exception) escape hands the
    client something it cannot classify, and a 503 then crashes the agent
    instead of degrading it -- neither fail-closed nor fail-open.
    """

    def __init__(self, status: int, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        message = f"control plane returned {status}"
        super().__init__(f"{message}: {detail}" if detail else message)


class ToolRefused(QuarantineError):
    """This tool is not available right now. The agent may do something else.

    Subclasses the package's own QuarantineError rather than declaring a second
    base of the same name -- two exception hierarchies both called
    QuarantineError would make `except QuarantineError` mean different things
    depending on which module the caller imported from.
    """


class RunFrozen(QuarantineError):
    """The run is suspended. The agent should stop."""


class Transport(Protocol):
    def post(self, path: str, body: dict) -> dict:
        """Post to the control plane and return the decoded JSON body.

        Two obligations, and the client can honour spec §9 only if both hold:

        * `ConnectionError` when the control plane is unreachable.
        * `TransportError(status, detail)` for any non-2xx response. Raising the
          HTTP library's own exception instead is what turns a 503 into a crashed
          agent: the client cannot tier on an exception type it does not know, so
          an outage of the *store* would escape `guard()` rather than degrade the
          worker to safe mode.

        Anything else that escapes `post` is a bug in the transport, and is
        allowed to propagate as one.
        """


class QuarantineClient:
    def __init__(self, run_id: str, transport: Transport) -> None:
        self._run_id = run_id
        self._transport = transport

    def guard(self, tool_name: str, args_digest: str, action: Callable[[], Any]) -> Any:
        """Gate a tool call, run it if permitted, and report the outcome."""
        decision = self.gate(tool_name, args_digest)
        if decision is Decision.FREEZE:
            raise RunFrozen(f"run {self._run_id} is frozen")
        if decision is Decision.DENY:
            raise ToolRefused(f"{tool_name} is not permitted right now")

        try:
            result = action()
        except Exception:
            self.report(tool_name, ok=False)
            raise
        self.report(tool_name, ok=True)
        return result

    def gate(self, tool_name: str, args_digest: str) -> Decision:
        try:
            response = self._post(
                "gate", {"tool_name": tool_name, "args_digest": args_digest}
            )
        except ConnectionError:
            return self._offline_decision(tool_name)
        except TransportError as exc:
            if exc.status in UNAVAILABLE_STATUSES:
                # A reachable server that cannot reach its own store is, from
                # here, indistinguishable from an unreachable one: no state, no
                # decision, no enforcement. Same tiering (spec §9).
                return self._offline_decision(tool_name)
            typed = _typed(exc, self._run_id)
            if typed is exc:
                raise
            raise typed from exc
        return Decision(response["decision"])

    def _offline_decision(self, tool_name: str) -> Decision:
        """The client-side half of the tiering.

        It matters precisely when the server cannot enforce anything: an outage
        degrades the agent to safe mode rather than stopping it or leaving it
        unguarded. Unclassified means HIGH here exactly as it does on the server
        -- a worker must not gain permissions by losing contact.
        """
        if registry.risk_of(tool_name) is ToolRisk.HIGH:
            return Decision.DENY
        return Decision.ALLOW

    def report(self, tool_name: str, ok: bool, tokens: int = 0, cost_cents: int = 0) -> None:
        self._try_post(
            "outcome",
            {"tool_name": tool_name, "ok": ok, "tokens": tokens, "cost_cents": cost_cents},
        )

    def heartbeat(self) -> None:
        self._try_post("heartbeat", {})

    def complete(self) -> None:
        self._try_post("complete", {})

    def _post(self, endpoint: str, body: dict) -> dict:
        return self._transport.post(f"/runs/{self._run_id}/{endpoint}", body)

    def _try_post(self, endpoint: str, body: dict) -> None:
        """Best-effort telemetry.

        An unreachable control plane must not crash the agent on a heartbeat or
        an outcome report -- the reaper already treats the resulting silence as
        misbehaviour, which is the correct response and does not need the
        worker's cooperation.

        Every error status is swallowed here too, not just the unreachable case:
        this path is telemetry, and there is no outcome report worth crashing a
        working agent over. `gate()` is where a status has to change a decision.
        """
        try:
            self._post(endpoint, body)
        except (ConnectionError, TransportError):
            pass


def _typed(exc: TransportError, run_id: str) -> QuarantineError:
    """Map a status the caller can act on to the package's own error.

    404 and 409 are answers, not outages, and a worker developer should catch
    `UnknownRun` rather than pattern-match on somebody's HTTP exception.
    """
    if exc.status == 404:
        return UnknownRun(run_id)
    if exc.status == 409:
        return RunTerminated(run_id)
    return exc
