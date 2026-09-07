import datetime
import json

import pytest
from codex_supervisor.models import ExitClassification
from codex_supervisor.parser import (
    classify_exit,
    detect_rate_limit,
    detect_rate_limit_stderr,
    parse_event,
    parse_reset_time,
)


def task_complete_event(error=None):
    return {
        "type": "event_msg",
        "payload": {"type": "task_complete", "turn_id": "t1", "error": error},
    }


def session_meta_event(session_id="abc-123"):
    return {
        "type": "session_meta",
        "payload": {"session_id": session_id, "cwd": "/tmp"},
    }


# --- parse_event ---

def test_parse_event_valid():
    line = '{"type": "event_msg", "payload": {}}'
    result = parse_event(line)
    assert result == {"type": "event_msg", "payload": {}}


def test_parse_event_invalid_returns_none():
    assert parse_event("not json {{") is None


def test_parse_event_empty_returns_none():
    assert parse_event("") is None


def test_parse_event_whitespace_returns_none():
    assert parse_event("   \n") is None


# --- detect_rate_limit ---

def test_detect_rate_limit_usage_exceeded():
    event = task_complete_event(error={
        "codex_error_info": "usage_limit_exceeded",
        "message": "You've hit your usage limit. Try again at 9:00 AM.",
    })
    result = detect_rate_limit(event)
    assert result is not None
    assert result.detected is True
    assert result.confidence == 1.0
    assert result.source == "jsonl_event"


def test_detect_rate_limit_no_error_returns_none():
    event = task_complete_event(error=None)
    assert detect_rate_limit(event) is None


def test_detect_rate_limit_other_event_returns_none():
    event = session_meta_event()
    assert detect_rate_limit(event) is None


def test_detect_rate_limit_text_pattern():
    event = task_complete_event(error={
        "codex_error_info": "other",
        "message": "Rate limit exceeded, please wait.",
    })
    result = detect_rate_limit(event)
    assert result is not None
    assert result.detected is True
    assert result.confidence == 0.7


def test_detect_rate_limit_stderr():
    result = detect_rate_limit_stderr("Error: usage limit reached, try again later")
    assert result is not None
    assert result.detected is True
    assert result.source == "stderr"


def test_detect_rate_limit_stderr_empty():
    assert detect_rate_limit_stderr("") is None


def test_detect_rate_limit_stderr_no_match():
    assert detect_rate_limit_stderr("normal operation log line") is None


# --- parse_reset_time ---

def test_parse_reset_time_am_pm():
    now = datetime.datetime.now(datetime.timezone.utc)
    # Use a time guaranteed to be in the future today or tomorrow
    future = now + datetime.timedelta(hours=2)
    time_str = future.strftime("%I:%M %p")
    msg = f"Try again at {time_str}."
    result = parse_reset_time(msg)
    assert result is not None
    assert result > now


def test_parse_reset_time_in_minutes():
    now = datetime.datetime.now(datetime.timezone.utc)
    result = parse_reset_time("Please try again in 30 minutes.")
    assert result is not None
    delta = result - now
    assert 29 * 60 <= delta.total_seconds() <= 31 * 60


def test_parse_reset_time_in_hours():
    now = datetime.datetime.now(datetime.timezone.utc)
    result = parse_reset_time("Resets in 2 hours.")
    assert result is not None
    delta = result - now
    assert 119 * 60 <= delta.total_seconds() <= 121 * 60


def test_parse_reset_time_iso():
    future = "2099-01-01T12:00:00Z"
    result = parse_reset_time(f"Resets at {future}")
    assert result is not None
    assert result.year == 2099


def test_parse_reset_time_unparseable():
    assert parse_reset_time("No time information here.") is None


def test_parse_reset_time_empty():
    assert parse_reset_time("") is None


# --- classify_exit ---

def test_classify_exit_normal():
    events = [task_complete_event(error=None)]
    assert classify_exit(events, 0, False) == ExitClassification.NORMAL_COMPLETION


def test_classify_exit_rate_limit():
    events = [task_complete_event(error={
        "codex_error_info": "usage_limit_exceeded",
        "message": "usage limit",
    })]
    assert classify_exit(events, 1, False) == ExitClassification.RATE_LIMIT


def test_classify_exit_interrupted():
    assert classify_exit([], 130, True) == ExitClassification.USER_INTERRUPT


def test_classify_exit_turn_aborted_interrupted():
    events = [{"type": "event_msg",
               "payload": {"type": "turn_aborted", "reason": "interrupted"}}]
    assert classify_exit(events, 130, False) == ExitClassification.USER_INTERRUPT


def test_classify_exit_transient_no_task_complete():
    events = [session_meta_event()]
    assert classify_exit(events, 1, False) == ExitClassification.TRANSIENT_ERROR


def test_classify_exit_zero_no_task_complete():
    # Unusual: exit 0 but no task_complete — treat as NORMAL_COMPLETION
    assert classify_exit([], 0, False) == ExitClassification.NORMAL_COMPLETION


# --- Public exec --json format ---

def test_detect_rate_limit_real_error_event():
    event = {"type": "error", "message": "You've hit your usage limit. try again at 7:05 AM."}
    result = detect_rate_limit(event)
    assert result is not None
    assert result.detected is True
    assert result.confidence == 1.0
    assert result.reset_at is not None


def test_detect_rate_limit_real_turn_failed():
    event = {"type": "turn.failed", "error": {"message": "usage limit exceeded"}}
    result = detect_rate_limit(event)
    assert result is not None
    assert result.detected is True


def test_classify_exit_real_turn_completed():
    events = [{"type": "turn.completed"}]
    assert classify_exit(events, 0, False) == ExitClassification.NORMAL_COMPLETION


def test_classify_exit_real_turn_failed_rate_limit():
    events = [{"type": "turn.failed", "error": {"message": "usage limit exceeded"}}]
    assert classify_exit(events, 1, False) == ExitClassification.RATE_LIMIT


def test_extract_session_id_thread_started():
    from codex_supervisor.parser import extract_session_id
    event = {"type": "thread.started", "thread_id": "abc-123"}
    assert extract_session_id(event) == "abc-123"


def test_extract_session_id_session_meta():
    from codex_supervisor.parser import extract_session_id
    event = {"type": "session_meta", "payload": {"session_id": "xyz-456"}}
    assert extract_session_id(event) == "xyz-456"


def test_extract_session_id_other_event():
    from codex_supervisor.parser import extract_session_id
    assert extract_session_id({"type": "turn.started"}) is None


def test_reset_local_clock_relative_to_original_error(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Taipei")
    reference = datetime.datetime(2026, 9, 6, 20, 0, tzinfo=datetime.timezone.utc)
    result = parse_reset_time("Try again after 06:30.", reference)
    assert result == datetime.datetime(2026, 9, 6, 22, 30, tzinfo=datetime.timezone.utc)


def test_reset_actual_codex_full_date(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Taipei")
    result = parse_reset_time("try again at Sep 7th, 2026 2:04 AM.")
    assert result == datetime.datetime(2026, 9, 6, 18, 4, tzinfo=datetime.timezone.utc)


@pytest.mark.parametrize("line", ["null", "[]", "123", '"error"'])
def test_non_object_json_is_not_an_event(line):
    assert parse_event(line) is None


def test_iso_fractional_seconds_retains_offset():
    value = parse_reset_time("Resets at 2026-09-07T20:00:00.500+08:00")
    assert value == datetime.datetime(2026, 9, 7, 12, 0, 0, 500000, tzinfo=datetime.timezone.utc)
