"""Settings.from_env."""

from __future__ import annotations

from datetime import timedelta

import pytest

from quarantine.config import Settings


class TestFromEnv:
    def test_defaults_when_the_environment_is_empty(self):
        settings = Settings.from_env({})
        assert settings.error_streak == 5
        assert settings.heartbeat_timeout == timedelta(minutes=2)

    def test_reads_integer_thresholds(self):
        assert Settings.from_env({"QUARANTINE_ERROR_STREAK": "9"}).error_streak == 9

    def test_reads_durations_in_seconds(self):
        settings = Settings.from_env({"QUARANTINE_HEARTBEAT_TIMEOUT": "30"})
        assert settings.heartbeat_timeout == timedelta(seconds=30)

    def test_reads_the_operator_token_and_database_path(self):
        settings = Settings.from_env(
            {"QUARANTINE_OPERATOR_TOKEN": "s3cret", "QUARANTINE_DATABASE_PATH": "/tmp/q.db"}
        )
        assert settings.operator_token == "s3cret"
        assert settings.database_path == "/tmp/q.db"


class TestJudgeBackgroundEveryGuard:
    def test_zero_is_rejected_rather_than_passed_through(self):
        with pytest.raises(ValueError):
            Settings.from_env({"QUARANTINE_JUDGE_BACKGROUND_EVERY": "0"})

    def test_negative_is_rejected_rather_than_passed_through(self):
        with pytest.raises(ValueError):
            Settings.from_env({"QUARANTINE_JUDGE_BACKGROUND_EVERY": "-3"})

    def test_positive_value_is_accepted(self):
        settings = Settings.from_env({"QUARANTINE_JUDGE_BACKGROUND_EVERY": "7"})
        assert settings.judge_background_every == 7


class TestJudgeTrajectoryEventsGuard:
    """The neighbour of `judge_background_every`, and unguarded until now.

    Both non-positive values fail silently and in opposite directions against
    SQLite's `LIMIT`: zero hands the judge an empty trajectory (which it will
    call CLEAR), a negative value hands it the entire run. Neither is visible in
    the test suite, because the in-memory fake slices `events[-0:]` -- which is
    `events[0:]`, i.e. everything -- and so shows the opposite behaviour to the
    store that actually ships.
    """

    def test_zero_is_rejected_rather_than_emptying_the_trajectory(self):
        with pytest.raises(ValueError):
            Settings.from_env({"QUARANTINE_JUDGE_TRAJECTORY_EVENTS": "0"})

    def test_negative_is_rejected_rather_than_meaning_unlimited(self):
        with pytest.raises(ValueError):
            Settings.from_env({"QUARANTINE_JUDGE_TRAJECTORY_EVENTS": "-1"})

    def test_the_message_names_the_variable(self):
        """An operator reading a crashed start-up needs the knob, not a stack."""
        with pytest.raises(ValueError, match="QUARANTINE_JUDGE_TRAJECTORY_EVENTS"):
            Settings.from_env({"QUARANTINE_JUDGE_TRAJECTORY_EVENTS": "0"})

    def test_positive_value_is_accepted(self):
        settings = Settings.from_env({"QUARANTINE_JUDGE_TRAJECTORY_EVENTS": "5"})
        assert settings.judge_trajectory_events == 5
