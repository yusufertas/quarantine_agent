"""Thresholds and timeouts. Defaults per spec §10.

The tool risk registry is deliberately NOT here -- it is version-controlled Python
in domain.registry, because which tools can hurt you is reviewed code, not
environment configuration (ADR-0003).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class Settings:
    # Rules (spec §7)
    loop_repeats: int = 3
    loop_window: int = 10
    error_streak: int = 5
    call_rate_per_minute: int = 60
    flap_degradations: int = 3
    flap_window: timedelta = timedelta(hours=1)

    # Recovery and liveness (spec §9, §10)
    recovery_interval: timedelta = timedelta(minutes=15)
    heartbeat_timeout: timedelta = timedelta(minutes=2)

    # Judge (spec §8)
    judge_timeout_seconds: float = 10.0
    judge_trajectory_events: int = 20
    judge_background_every: int = 10

    # Operator auth and storage (spec §6, §10)
    operator_token: str = ""
    database_path: str = "quarantine.db"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        raise NotImplementedError("Settings.from_env")


DEFAULTS = Settings()
