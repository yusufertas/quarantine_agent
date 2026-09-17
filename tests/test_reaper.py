"""The heartbeat reaper (spec §9).

The backstop that makes a cooperative protocol defensible: the agent most in need of
quarantine is the one least likely to keep asking permission, so silence escalates
rather than being ignored (ADR-0001).

Every test here drives a FakeClock. Nothing sleeps.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import T0, make_run
from quarantine.config import Settings
from quarantine.domain.states import RunState
from quarantine.reaper import Reaper

TIMEOUT = Settings().heartbeat_timeout


@pytest.fixture
def reaper(store, clock, settings):
    return Reaper(store=store, clock=clock, settings=settings)


class TestStaleHeartbeats:
    def test_silent_run_is_escalated(self, store, clock, reaper):
        store.create_run(make_run(state=RunState.HEALTHY, last_heartbeat_at=T0))
        clock.advance(TIMEOUT + timedelta(seconds=1))
        assert reaper.sweep() == 1
        assert store.get_run("run-1").state is RunState.DEGRADED

    def test_live_run_is_left_alone(self, store, clock, reaper):
        store.create_run(make_run(state=RunState.HEALTHY, last_heartbeat_at=T0))
        clock.advance(TIMEOUT - timedelta(seconds=1))
        assert reaper.sweep() == 0
        assert store.get_run("run-1").state is RunState.HEALTHY

    def test_one_rung_per_pass(self, store, clock, reaper):
        """Consistent with every other escalation: two passes to reach FROZEN, and
        the audit trail shows both steps instead of an inferred jump."""
        store.create_run(make_run(state=RunState.HEALTHY, last_heartbeat_at=T0))
        clock.advance(TIMEOUT + timedelta(seconds=1))
        reaper.sweep()
        assert store.get_run("run-1").state is RunState.DEGRADED
        clock.advance(TIMEOUT + timedelta(seconds=1))
        reaper.sweep()
        assert store.get_run("run-1").state is RunState.FROZEN

    def test_frozen_run_is_not_escalated_further_by_silence(self, store, clock, reaper):
        """A frozen run is supposed to be silent. Terminating it is a human decision."""
        store.create_run(make_run(state=RunState.FROZEN, last_heartbeat_at=T0))
        clock.advance(timedelta(days=7))
        assert reaper.sweep() == 0
        assert store.get_run("run-1").state is RunState.FROZEN

    def test_terminated_runs_are_ignored(self, store, clock, reaper):
        store.create_run(make_run(state=RunState.TERMINATED, last_heartbeat_at=T0))
        clock.advance(timedelta(days=7))
        assert reaper.sweep() == 0


class TestCause:
    def test_cause_says_the_worker_went_silent(self, store, clock, reaper):
        """Naming a detector that never fired would mislead whoever reads the trail."""
        store.create_run(make_run(last_heartbeat_at=T0))
        clock.advance(TIMEOUT + timedelta(seconds=1))
        reaper.sweep()
        transition = store.transitions("run-1")[-1]
        assert transition.cause == "heartbeat_timeout"
        assert transition.actor == "system"


class TestSweepScope:
    def test_sweeps_every_stale_run(self, store, clock, reaper):
        for i in range(3):
            store.create_run(make_run(run_id=f"run-{i}", last_heartbeat_at=T0))
        store.create_run(make_run(run_id="fresh", last_heartbeat_at=T0 + timedelta(hours=1)))
        clock.advance(TIMEOUT + timedelta(seconds=1))
        assert reaper.sweep() == 3
        assert store.get_run("fresh").state is RunState.HEALTHY

    def test_sweep_is_idempotent_within_one_timeout(self, store, clock, reaper):
        """A second pass before any new silence must not double-escalate."""
        store.create_run(make_run(last_heartbeat_at=T0))
        clock.advance(TIMEOUT + timedelta(seconds=1))
        reaper.sweep()
        assert reaper.sweep() == 0
        assert store.get_run("run-1").state is RunState.DEGRADED
