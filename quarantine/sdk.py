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
from .errors import QuarantineError


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
        """Raises ConnectionError when the control plane is unreachable."""


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
            # The client-side half of the tiering. It matters precisely when
            # the server cannot enforce anything: an outage degrades the agent
            # to safe mode rather than stopping it or leaving it unguarded.
            if registry.risk_of(tool_name) is ToolRisk.HIGH:
                return Decision.DENY
            return Decision.ALLOW
        return Decision(response["decision"])

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
        """
        try:
            self._post(endpoint, body)
        except ConnectionError:
            pass
