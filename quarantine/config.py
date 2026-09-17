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
        source = os.environ if env is None else env
        defaults = cls()

        def integer(name: str, fallback: int) -> int:
            raw = source.get(f"QUARANTINE_{name}")
            return int(raw) if raw else fallback

        def duration(name: str, fallback: timedelta) -> timedelta:
            raw = source.get(f"QUARANTINE_{name}")
            return timedelta(seconds=int(raw)) if raw else fallback

        judge_background_every = integer(
            "JUDGE_BACKGROUND_EVERY", defaults.judge_background_every
        )
        if judge_background_every <= 0:
            raise ValueError(
                "QUARANTINE_JUDGE_BACKGROUND_EVERY must be a positive integer, "
                f"got {judge_background_every!r} (the gate divides by it)"
            )

        return cls(
            loop_repeats=integer("LOOP_REPEATS", defaults.loop_repeats),
            loop_window=integer("LOOP_WINDOW", defaults.loop_window),
            error_streak=integer("ERROR_STREAK", defaults.error_streak),
            call_rate_per_minute=integer(
                "CALL_RATE_PER_MINUTE", defaults.call_rate_per_minute
            ),
            flap_degradations=integer("FLAP_DEGRADATIONS", defaults.flap_degradations),
            flap_window=duration("FLAP_WINDOW", defaults.flap_window),
            recovery_interval=duration("RECOVERY_INTERVAL", defaults.recovery_interval),
            heartbeat_timeout=duration("HEARTBEAT_TIMEOUT", defaults.heartbeat_timeout),
            judge_timeout_seconds=float(
                source.get("QUARANTINE_JUDGE_TIMEOUT_SECONDS")
                or defaults.judge_timeout_seconds
            ),
            judge_trajectory_events=integer(
                "JUDGE_TRAJECTORY_EVENTS", defaults.judge_trajectory_events
            ),
            judge_background_every=judge_background_every,
            operator_token=source.get("QUARANTINE_OPERATOR_TOKEN", defaults.operator_token),
            database_path=source.get("QUARANTINE_DATABASE_PATH", defaults.database_path),
        )


DEFAULTS = Settings()
