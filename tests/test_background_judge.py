"""Out-of-band judging: the path `main.build()` wires and nothing exercised.

Spec §8: LOW-risk trajectories are judged out of band every `J` gate calls, the
verdict is binding and applies from the next gate call, and every invocation --
including a failure -- appends a JUDGE event. `Gate._maybe_judge_in_background`
and `Gate._judge_now` implement that, and until this file no test injected a
scheduler, so the entire branch ran only in production. A background verdict
that escalated the wrong run, or a judge outage that raised inside the thread
pool, would have shipped green.

The scheduler here is a list. A queued job is a closure; running it is the test's
decision, which is what makes "the verdict applies from the NEXT call" checkable
rather than racy.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import FakeJudge, UnavailableJudge, call, make_run
from quarantine.config import Settings
from quarantine.domain import machine
from quarantine.domain.models import Event, Verdict
from quarantine.domain.states import Decision, EventKind, RunState, Severity
from quarantine.gate import Gate
from quarantine.sqlite_store import SqliteRepository

LOW = "search"
HIGH = "issue_payment"


@pytest.fixture
def queue() -> list:
    return []


@pytest.fixture
def gate_for(store, clock, queue):
    """A gate whose scheduler is `queue.append`, judging on EVERY call.

    `judge_background_every=1` makes `next_seq % every == 0` true on each gate
    call, so these tests are about what a queued job does -- the cadence is a
    separate question, pinned in `TestCadence`.
    """

    def build(judge=None, *, every: int = 1) -> Gate:
        return Gate(
            store=store,
            judge=judge or FakeJudge(),
            clock=clock,
            settings=Settings(operator_token="test-token", judge_background_every=every),
            background=queue.append,
        )

    return build


def run_queued(queue: list) -> int:
    """Drain the scheduler, returning how many jobs ran."""
    jobs, queue[:] = list(queue), []
    for job in jobs:
        job()
    return len(jobs)


class TestSchedulerPlacement:
    def test_no_scheduler_means_no_background_work(self, store, clock):
        """The documented default: without an injected scheduler the LOW path
        touches nothing but storage. This is the property the unit suite has
        silently relied on all along."""
        store.create_run(make_run())
        judge = FakeJudge()
        gate = Gate(
            store=store,
            judge=judge,
            clock=clock,
            settings=Settings(operator_token="test-token", judge_background_every=1),
        )
        gate.decide("run-1", call(LOW))
        assert judge.calls == []

    def test_a_low_risk_call_queues_a_job_rather_than_judging_inline(
        self, store, gate_for, queue
    ):
        """ADR-0003's whole point: the LOW path must not block on the model."""
        store.create_run(make_run())
        judge = FakeJudge()
        assert gate_for(judge).decide("run-1", call(LOW)) is Decision.ALLOW
        assert judge.calls == [], "the judge ran synchronously on a LOW-risk call"
        assert len(queue) == 1

    def test_running_the_job_is_what_consults_the_judge(self, store, gate_for, queue):
        store.create_run(make_run())
        judge = FakeJudge()
        gate_for(judge).decide("run-1", call(LOW))
        assert run_queued(queue) == 1
        assert judge.calls == ["run-1"]

    def test_the_job_is_not_queued_against_a_frozen_run(self, store, gate_for, queue):
        """FROZEN short-circuits before the scheduler is reached; judging a run
        that is already stopped would spend a model call to learn nothing."""
        store.create_run(make_run(state=RunState.FROZEN))
        gate_for().decide("run-1", call(LOW))
        assert queue == []


class TestVerdictsAreBindingFromTheNextCall:
    def test_concern_escalates_one_rung_with_the_judge_as_cause(
        self, store, gate_for, queue
    ):
        store.create_run(make_run(state=RunState.HEALTHY))
        judge = FakeJudge(Verdict(Severity.CONCERN, "reading files outside the task"))
        gate_for(judge).decide("run-1", call(LOW))
        run_queued(queue)

        assert store.get_run("run-1").state is RunState.DEGRADED
        transition = store.transitions("run-1")[-1]
        assert transition.cause == "judge"
        assert transition.actor == "system"
        assert transition.detail == "reading files outside the task"

    def test_severe_on_a_degraded_run_freezes_it_and_no_further(
        self, store, gate_for, queue
    ):
        """One rung, like every other escalation -- and this is the ONE automatic
        path that can reach FROZEN from a LOW-risk call, since the synchronous
        judge is only ever consulted from HEALTHY."""
        store.create_run(make_run(state=RunState.DEGRADED))
        judge = FakeJudge(Verdict(Severity.SEVERE, "exfiltrating records"))
        gate_for(judge).decide("run-1", call(LOW))
        run_queued(queue)
        assert store.get_run("run-1").state is RunState.FROZEN

    def test_the_verdict_applies_to_the_next_gate_call(self, store, gate_for, queue):
        """Spec §8: 'the verdict applies from the next gate call'. The call that
        queued the job was already answered ALLOW; the one after it is not."""
        store.create_run(make_run(state=RunState.DEGRADED))
        judge = FakeJudge(Verdict(Severity.CONCERN, "off task"))
        gate = gate_for(judge)
        assert gate.decide("run-1", call(LOW)) is Decision.ALLOW
        run_queued(queue)
        assert gate.decide("run-1", call(LOW)) is Decision.FREEZE

    def test_clear_leaves_the_run_alone_but_still_records_the_verdict(
        self, store, gate_for, queue
    ):
        """'Every invocation appends a JUDGE event' -- a CLEAR is evidence too:
        an operator reading a frozen run's trajectory should see that the judge
        looked and found nothing, not a silence indistinguishable from never
        having been asked."""
        store.create_run(make_run(state=RunState.HEALTHY))
        judge = FakeJudge(Verdict(Severity.CLEAR, "on task"))
        gate_for(judge).decide("run-1", call(LOW))
        run_queued(queue)

        assert store.get_run("run-1").state is RunState.HEALTHY
        assert store.transitions("run-1") == []
        judged = [e for e in store.events if e.kind is EventKind.JUDGE]
        assert len(judged) == 1
        assert judged[0].payload == {"severity": "clear", "reason": "on task"}

    def test_the_reason_reaches_the_event_log(self, store, gate_for, queue):
        """The reason is what the operator reads when deciding on release."""
        store.create_run(make_run())
        judge = FakeJudge(Verdict(Severity.CONCERN, "deleting unrelated records"))
        gate_for(judge).decide("run-1", call(LOW))
        run_queued(queue)
        judged = [e for e in store.events if e.kind is EventKind.JUDGE]
        assert judged[-1].payload["reason"] == "deleting unrelated records"


class TestStateIsReadWhenTheJobRuns:
    """The job closes over the run ID, not the run. Between queueing and running,
    the worker's next call, the reaper or an operator may have moved the run --
    and the verdict must land on the run as it is, not as it was."""

    def test_a_run_frozen_in_the_meantime_is_not_escalated_again(
        self, store, gate_for, queue
    ):
        """FROZEN is not in the escalation table; the job must skip rather than
        raise `IllegalTransition` from inside a thread pool, where nobody would
        see it."""
        store.create_run(make_run(state=RunState.HEALTHY))
        judge = FakeJudge(Verdict(Severity.SEVERE, "clearly off task"))
        gate_for(judge).decide("run-1", call(LOW))

        frozen = make_run(state=RunState.FROZEN)
        store.save_run(frozen)              # the fake's raw state-setter
        run_queued(queue)

        assert store.get_run("run-1").state is RunState.FROZEN
        assert store.transitions("run-1") == []

    def test_a_run_terminated_in_the_meantime_is_left_terminated(
        self, store, gate_for, queue
    ):
        store.create_run(make_run(state=RunState.HEALTHY))
        judge = FakeJudge(Verdict(Severity.CONCERN, "off task"))
        gate_for(judge).decide("run-1", call(LOW))
        store.save_run(make_run(state=RunState.TERMINATED))
        run_queued(queue)
        assert store.get_run("run-1").state is RunState.TERMINATED
        assert store.transitions("run-1") == []

    def test_a_run_escalated_by_a_rule_in_the_meantime_moves_only_one_more_rung(
        self, store, gate_for, queue
    ):
        """Queued from HEALTHY, run after a rule took it to DEGRADED: the verdict
        escalates from where the run IS, so it lands on FROZEN -- still exactly
        one rung from the state the job observed, never two from the state it
        was queued under."""
        store.create_run(make_run(state=RunState.HEALTHY))
        judge = FakeJudge(Verdict(Severity.CONCERN, "off task"))
        gate_for(judge).decide("run-1", call(LOW))
        store.save_run(make_run(state=RunState.DEGRADED))
        run_queued(queue)
        assert store.get_run("run-1").state is RunState.FROZEN
        assert [t.from_state for t in store.transitions("run-1")] == [RunState.DEGRADED]


class TestJudgeOutageInTheBackground:
    """Spec §9, LOW row: 'logged, background judging skipped'."""

    def test_an_unavailable_judge_does_not_raise_out_of_the_job(
        self, store, gate_for, queue
    ):
        """A job that raised would die silently inside `ThreadPoolExecutor.submit`
        and the outage would be invisible. Instead it is logged and swallowed."""
        store.create_run(make_run())
        gate_for(UnavailableJudge()).decide("run-1", call(LOW))
        run_queued(queue)                   # must not raise

    def test_the_outage_is_recorded_as_an_unavailable_judge_event(
        self, store, gate_for, queue
    ):
        """'Judge errors are never swallowed' means never swallowed SILENTLY."""
        store.create_run(make_run())
        gate_for(UnavailableJudge()).decide("run-1", call(LOW))
        run_queued(queue)
        judged = [e for e in store.events if e.kind is EventKind.JUDGE]
        assert len(judged) == 1
        assert judged[0].payload["available"] is False
        assert "timed out" in judged[0].payload["error"]

    def test_an_outage_never_escalates(self, store, gate_for, queue):
        """Fail closed applies to the HIGH-risk call in flight; a LOW-risk
        trajectory nobody could assess is not evidence of misbehaviour."""
        store.create_run(make_run())
        gate_for(UnavailableJudge()).decide("run-1", call(LOW))
        run_queued(queue)
        assert store.get_run("run-1").state is RunState.HEALTHY
        assert store.transitions("run-1") == []


class TestCadence:
    """Spec §8: out-of-band judging happens 'every J=10 gate calls'.

    Before `count_proposals`, the gate decided when to queue by
    `next_seq % judge_background_every`, and `next_seq` counted LOG ROWS --
    the same rows-versus-calls confusion `_history_limit` and `rules.loop`
    each carry a long comment about. A worker that reported outcomes (3 rows
    per LOW call) was judged every 10 calls by arithmetic coincidence; one that
    did not (2 rows per call) was judged every 5; a HIGH-only stream with
    outcomes was never judged out of band at all. The cadence is now counted
    in LOW proposals, so both worker shapes below must agree with the spec.
    `tests/test_background_cadence.py` pins the same property on SQLite.
    """

    J = 10

    def drive(self, store, gate, queue, *, calls: int, report_outcomes: bool) -> list[int]:
        """Gate `calls` distinct LOW-risk calls; return the 1-based call numbers
        on which a background job was queued."""
        queued_on = []
        for i in range(1, calls + 1):
            before = len(queue)
            gate.decide("run-1", call(LOW, args_digest=f"d{i}"))
            if len(queue) > before:
                queued_on.append(i)
            if report_outcomes:
                store.append_event(
                    Event(
                        run_id="run-1",
                        seq=store.next_seq("run-1"),
                        kind=EventKind.OUTCOME,
                        created_at=gate._clock.now(),
                        tool_name=LOW,
                        payload={"ok": True},
                    )
                )
        return queued_on

    @pytest.mark.parametrize("report_outcomes", [True, False])
    def test_every_jth_low_risk_call_queues_a_job_whatever_the_worker_reports(
        self, store, gate_for, queue, report_outcomes
    ):
        store.create_run(make_run(budget_tokens=10**9, deadline_after=timedelta(days=1)))
        queued_on = self.drive(
            store,
            gate_for(every=self.J),
            queue,
            calls=4 * self.J,
            report_outcomes=report_outcomes,
        )
        assert queued_on == [self.J, 2 * self.J, 3 * self.J, 4 * self.J]

    def test_a_synchronously_judged_high_risk_call_is_not_queued_again(
        self, store, gate_for, queue
    ):
        """It was just judged; a second, out-of-band verdict on the same
        trajectory would spend a model call to learn nothing new."""
        store.create_run(make_run())
        judge = FakeJudge()
        gate_for(judge).decide("run-1", call(HIGH))
        assert judge.calls == ["run-1"]
        assert queue == []


class TestAStaleVerdictNeverDeEscalates:
    """The race the audit found: the job reads the run, the model takes
    seconds, and in those seconds a rule, the reaper or an operator moves the
    run. Escalating from the snapshot wrote DEGRADED over FROZEN -- an
    automatic path clearing a containment it did not set, measured before the
    fix. The write now carries the snapshot's revision and the store refuses a
    stale one; the job records that its verdict was discarded."""

    @pytest.fixture(params=["in_memory", "sqlite"])
    def repo(self, request, tmp_path, store):
        if request.param == "in_memory":
            return store
        real = SqliteRepository(str(tmp_path / "stale.db"))
        real.initialize()
        return real

    def test_a_run_frozen_during_evaluation_stays_frozen(self, repo, clock, queue):
        repo.create_run(make_run())

        class FreezesWhileThinking(FakeJudge):
            def evaluate(self, run, trajectory):
                current = repo.get_run(run.id)
                for cause in ("loop", "loop"):
                    current = repo.record_state_change(
                        *machine.escalate(current, cause=cause, actor="system", now=clock.now())
                    )
                return Verdict(Severity.CONCERN, "verdict from before the freeze")

        gate = Gate(
            store=repo,
            judge=FreezesWhileThinking(),
            clock=clock,
            settings=Settings(operator_token="test-token", judge_background_every=1),
            background=queue.append,
        )
        gate.decide("run-1", call(LOW))
        run_queued(queue)

        assert repo.get_run("run-1").state is RunState.FROZEN
        assert [t.cause for t in repo.transitions("run-1")] == ["loop", "loop"]
        kinds = [e.kind for e in repo.recent_events("run-1", 10)]
        assert kinds[-1] is EventKind.SYSTEM
        assert repo.recent_events("run-1", 1)[0].payload == {
            "reason": "stale_judge_verdict_discarded"
        }
