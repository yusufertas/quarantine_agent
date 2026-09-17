"""The tool risk registry (ADR-0003).

Small file, disproportionate importance: the registry is the single table behind
judge placement, DEGRADED permissions, and fail-open/fail-closed behaviour. Its
default is the thing most likely to be "fixed" into a bug.
"""

from __future__ import annotations

import pytest

from quarantine.domain import registry
from quarantine.domain.states import ToolRisk


class TestKnownTools:
    @pytest.mark.parametrize("tool", ["search", "read_file", "fetch_url", "list_records"])
    def test_read_only_tools_are_low_risk(self, tool):
        assert registry.risk_of(tool) is ToolRisk.LOW

    @pytest.mark.parametrize(
        "tool", ["write_file", "delete_records", "send_email", "issue_payment", "run_shell"]
    )
    def test_irreversible_tools_are_high_risk(self, tool):
        assert registry.risk_of(tool) is ToolRisk.HIGH


class TestUnclassifiedTools:
    def test_unknown_tool_resolves_to_high(self):
        """Not a convenience. A permissive default lets the registry rot silently
        into a permit-list for every tool anyone adds later."""
        assert registry.risk_of("some_tool_added_next_tuesday") is ToolRisk.HIGH

    @pytest.mark.parametrize("name", ["", "   ", "SEARCH"])
    def test_near_misses_are_not_quietly_matched(self, name):
        """Case and whitespace must not smuggle a HIGH tool into the LOW column."""
        assert registry.risk_of(name) is ToolRisk.HIGH

    def test_unclassified_reports_names_missing_from_the_registry(self):
        missing = registry.unclassified(["search", "brand_new_tool", "send_email"])
        assert set(missing) == {"brand_new_tool"}


class TestRegistryContents:
    def test_every_entry_has_a_risk_level(self):
        assert all(isinstance(v, ToolRisk) for v in registry.TOOL_RISKS.values())

    def test_registry_is_not_empty(self):
        assert registry.TOOL_RISKS
