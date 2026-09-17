"""Production entrypoint.

Run with:  uvicorn quarantine.main:build --factory
"""

from __future__ import annotations

import anthropic
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI

from .api import create_app
from .clock import SystemClock
from .config import Settings
from .domain.registry import TOOL_RISKS
from .judge import ClaudeJudge
from .sqlite_store import SqliteRepository


def build() -> FastAPI:
    settings = Settings.from_env()
    if not settings.operator_token:
        raise RuntimeError(
            "QUARANTINE_OPERATOR_TOKEN is unset; refusing to start with an "
            "operator surface nobody can authenticate to"
        )

    store = SqliteRepository(settings.database_path)
    store.initialize()

    judge = ClaudeJudge(
        client=anthropic.Anthropic(), timeout_seconds=settings.judge_timeout_seconds
    )
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="judge")

    print(f"tool risk registry: {len(TOOL_RISKS)} tools classified")
    return create_app(
        store=store,
        judge=judge,
        clock=SystemClock(),
        settings=settings,
        background=executor.submit,
    )
