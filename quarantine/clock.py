"""Time is injected, never read ambiently.

Every component that needs the current time takes a Clock. This is what lets the
reaper's heartbeat-timeout tests run instantly instead of sleeping (spec §11).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """The real clock. Always timezone-aware UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)
