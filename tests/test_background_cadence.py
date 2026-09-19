"""Background sampling counts LOW proposals, not unrelated trajectory rows."""

from conftest import FakeClock, FakeJudge, make_event, make_run
from quarantine.config import Settings
from quarantine.domain.models import ToolCall
from quarantine.domain.states import Decision, EventKind
from quarantine.gate import Gate
from quarantine.sqlite_store import SqliteRepository
import pytest


@pytest.mark.parametrize("every", [3, 10])
@pytest.mark.parametrize("outcomes", [False, True])
@pytest.mark.parametrize("drain_each_call", [False, True])
def test_low_judge_cadence_is_independent_of_telemetry_and_callback_timing(
    tmp_path, every, outcomes, drain_each_call,
):
    repo = SqliteRepository(str(tmp_path / "cadence.db"))
    repo.initialize()
    repo.create_run(make_run())
    pending, scheduled = [], []
    current_call = 0

    def schedule(callback):
        scheduled.append(current_call)
        pending.append(callback)

    judge = FakeJudge()
    gate = Gate(repo, judge, FakeClock(), Settings(judge_background_every=every), schedule)
    for current_call in range(1, 21):
        assert gate.decide("run-1", ToolCall("search", str(current_call))) is Decision.ALLOW
        if outcomes:
            repo.append_event(make_event(kind=EventKind.OUTCOME, ok=True))
        if drain_each_call:
            while pending:
                pending.pop(0)()
    assert scheduled == list(range(every, 21, every))
    while pending:
        pending.pop(0)()
    assert len(judge.calls) == len(scheduled)


def test_high_risk_calls_are_not_judged_twice(tmp_path):
    repo = SqliteRepository(str(tmp_path / "high.db"))
    repo.initialize()
    repo.create_run(make_run())
    pending = []
    judge = FakeJudge()
    gate = Gate(repo, judge, FakeClock(), Settings(judge_background_every=3), pending.append)
    for i in range(20):
        assert gate.decide("run-1", ToolCall("issue_payment", str(i))) is Decision.ALLOW
    assert len(judge.calls) == 20
    assert pending == []


def test_interleaved_high_proposals_do_not_shift_low_cadence(tmp_path):
    repo = SqliteRepository(str(tmp_path / "mixed.db"))
    repo.initialize()
    repo.create_run(make_run())
    pending = []
    gate = Gate(repo, FakeJudge(), FakeClock(), Settings(judge_background_every=3), pending.append)
    for i in range(6):
        gate.decide("run-1", ToolCall("search", str(i)))
        gate.decide("run-1", ToolCall("issue_payment", str(i)))
    assert len(pending) == 2
