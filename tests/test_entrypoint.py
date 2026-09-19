"""`quarantine.main.build()` and `SystemClock`: the two files at 0% coverage.

`build()` is what `uvicorn quarantine.main:build --factory` runs. Every test
before this one constructed `create_app` directly with injected collaborators,
so the production wiring -- env-driven settings, the operator-token refusal, the
SQLite store on the configured path, the judge with the configured timeout, and
the thread pool that makes background judging exist at all -- ran only in
production. `CLAUDE.md` states that `main.build()` supplies a scheduler; nothing
checked it.

No network: the Anthropic client is constructed with a placeholder key and never
called (only LOW-risk calls are gated, and background judging is either
disabled by cadence or handed a fake judge).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import quarantine.main as main
from conftest import FakeJudge
from quarantine.clock import SystemClock

OPERATOR = {"X-Operator-Token": "from-the-environment", "X-Operator-Id": "operator-1"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A clean QUARANTINE_* environment with a token, a tmp database, and a
    placeholder API key -- the minimum `build()` accepts."""
    for name in list(os.environ):
        if name.startswith("QUARANTINE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-never-sent")
    monkeypatch.setenv("QUARANTINE_OPERATOR_TOKEN", "from-the-environment")
    monkeypatch.setenv("QUARANTINE_DATABASE_PATH", str(tmp_path / "boot.db"))
    # Never queue a background judge call against the real client.
    monkeypatch.setenv("QUARANTINE_JUDGE_BACKGROUND_EVERY", "1000000")
    return monkeypatch


class TestSystemClock:
    def test_is_timezone_aware_utc(self):
        """Every stored timestamp is ISO text compared as text by the reaper's
        query. A naive or non-UTC clock would sort wrongly and silently."""
        now = SystemClock().now()
        assert now.tzinfo is UTC

    def test_does_not_go_backwards(self):
        clock = SystemClock()
        assert clock.now() <= clock.now()


class TestRefusalToBoot:
    def test_no_operator_token_means_no_service(self, env):
        """An operator surface nobody can authenticate to is a frozen run nobody
        can release. Refusing at boot is the only safe answer."""
        env.delenv("QUARANTINE_OPERATOR_TOKEN")
        with pytest.raises(RuntimeError) as excinfo:
            main.build()
        assert "QUARANTINE_OPERATOR_TOKEN" in str(excinfo.value)

    def test_an_empty_token_is_no_token(self, env):
        env.setenv("QUARANTINE_OPERATOR_TOKEN", "")
        with pytest.raises(RuntimeError):
            main.build()

    def test_a_rejected_setting_is_rejected_at_boot(self, env):
        """`Settings.from_env` guards two values; `build()` must let those
        refusals through rather than serving with a value the gate divides by."""
        env.setenv("QUARANTINE_JUDGE_BACKGROUND_EVERY", "0")
        with pytest.raises(ValueError):
            main.build()


class TestTheBootedService:
    def test_builds_an_app_over_the_configured_database(self, env, tmp_path):
        app = main.build()
        assert isinstance(app, FastAPI)
        with sqlite3.connect(tmp_path / "boot.db") as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        assert {"runs", "events", "transitions"} <= tables

    def test_the_operator_token_is_the_one_from_the_environment(self, env):
        http = TestClient(main.build())
        assert http.get("/runs").status_code == 401
        assert http.get("/runs", headers=OPERATOR).status_code == 200

    def test_a_worker_can_register_and_be_gated(self, env):
        """The smallest end-to-end proof that the wiring is complete: a real
        store, a real clock, a run created and a LOW-risk call answered without
        touching the judge."""
        http = TestClient(main.build())
        created = http.post(
            "/runs",
            json={
                "agent_name": "boot-check",
                "budget_tokens": 1_000,
                "budget_cost_cents": 100,
                "deadline_seconds": 60,
            },
        )
        assert created.status_code == 201, created.text
        run_id = created.json()["id"]
        gated = http.post(
            f"/runs/{run_id}/gate", json={"tool_name": "search", "args_digest": "d1"}
        )
        assert gated.json() == {"decision": "allow"}
        detail = http.get(f"/runs/{run_id}", headers=OPERATOR).json()
        assert detail["state"] == "healthy"
        assert detail["budget_tokens"] == 1_000

    def test_settings_are_read_when_build_is_called(self, env):
        """A factory that captured the environment at import time would serve
        the first process's token to every later one."""
        first = TestClient(main.build())
        env.setenv("QUARANTINE_OPERATOR_TOKEN", "rotated")
        second = TestClient(main.build())
        assert first.get("/runs", headers=OPERATOR).status_code == 200
        assert second.get("/runs", headers=OPERATOR).status_code == 401
        assert second.get(
            "/runs", headers={"X-Operator-Token": "rotated", "X-Operator-Id": "o"}
        ).status_code == 200


class TestJudgeWiring:
    """`build()` is the only place a scheduler is supplied. Swap the judge for a
    recording fake and the thread pool for one that runs inline, and the
    background path in `gate.py` becomes observable from the outside."""

    @pytest.fixture
    def wired(self, env, monkeypatch):
        judge = FakeJudge()
        constructed: list[dict] = []

        def fake_judge(**kwargs):
            constructed.append(kwargs)
            return judge

        class InlineExecutor:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def submit(self, fn, *args):
                fn(*args)

        monkeypatch.setattr(main, "ClaudeJudge", fake_judge)
        monkeypatch.setattr(main, "ThreadPoolExecutor", InlineExecutor)
        env.setenv("QUARANTINE_JUDGE_BACKGROUND_EVERY", "1")
        env.setenv("QUARANTINE_JUDGE_TIMEOUT_SECONDS", "2.5")
        return judge, constructed

    def test_the_judge_gets_the_configured_timeout(self, wired):
        _, constructed = wired
        main.build()
        assert constructed[0]["timeout_seconds"] == 2.5

    def test_a_low_risk_call_is_judged_out_of_band(self, wired):
        """With a scheduler present, the LOW path queues work and the executor
        runs it: the judge sees the run. Without the scheduler `build()`
        supplies, this call would never reach a judge at all."""
        judge, _ = wired
        http = TestClient(main.build())
        run_id = http.post(
            "/runs",
            json={
                "agent_name": "a",
                "budget_tokens": 1_000,
                "budget_cost_cents": 100,
                "deadline_seconds": 60,
            },
        ).json()["id"]
        http.post(f"/runs/{run_id}/gate", json={"tool_name": "search", "args_digest": "d1"})
        assert judge.calls == [run_id]
