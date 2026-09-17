"""Tool name -> blast radius.

One table, four consumers (ADR-0003): whether the judge runs synchronously, whether
a call survives DEGRADED, whether an unreachable dependency fails open or closed,
and what an operator reads when asking "what can this agent do to us".

An unclassified tool resolves to HIGH. That is a correctness property, not a
convenience -- a permissive default would let the registry rot silently into a
permit-list for anything newly added.
"""

from __future__ import annotations

from collections.abc import Iterable

from .states import ToolRisk

TOOL_RISKS: dict[str, ToolRisk] = {
    # Read-only, cheap, reversible.
    "search": ToolRisk.LOW,
    "read_file": ToolRisk.LOW,
    "fetch_url": ToolRisk.LOW,
    "list_records": ToolRisk.LOW,
    # Not cheaply reversible.
    "write_file": ToolRisk.HIGH,
    "delete_records": ToolRisk.HIGH,
    "send_email": ToolRisk.HIGH,
    "issue_payment": ToolRisk.HIGH,
    "run_shell": ToolRisk.HIGH,
}


def risk_of(tool_name: str) -> ToolRisk:
    """Resolve a tool's risk. Unknown tools are HIGH.

    Exact-match lookup on purpose. "SEARCH" and " search " are not `search`;
    normalising them would let a typo in a tool name silently inherit a LOW
    classification that nobody reviewed.
    """
    return TOOL_RISKS.get(tool_name, ToolRisk.HIGH)


def unclassified(tool_names: Iterable[str]) -> tuple[str, ...]:
    """Names absent from the registry, for the startup check."""
    return tuple(name for name in tool_names if name not in TOOL_RISKS)
