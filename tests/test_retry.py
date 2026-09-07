import datetime

import pytest
from codex_supervisor.models import (
    ExitClassification,
    Job,
    JobStatus,
    RateLimitEvent,
    SupervisorConfig,
)
from codex_supervisor.retry import decide_retry
from pathlib import Path


def _config() -> SupervisorConfig:
    return SupervisorConfig(
        state_dir=Path("/tmp/test-state"),
        fallback_wait_minutes=30,
        max_crash_retries=5,
        max_unknown_retries=3,
        backoff_base_seconds=30,
    )


def _job(**kwargs) -> Job:
    defaults = dict(
        job_id="sv-test",
        status=JobStatus.RUNNING,
        codex_command=["codex"],
        work_dir="/tmp",
        created_at="2026-09-06T00:00:00Z",
        retry_count=0,
        rate_limit_retries=0,
    )
    defaults.update(kwargs)
    return Job(**defaults)


def _rl(reset_at=None, raw="rate limit") -> RateLimitEvent:
    return RateLimitEvent(
        detected=True,
        reset_at=reset_at,
        raw_message=raw,
        confidence=1.0,
        source="test",
    )


def test_no_retry_on_completion():
    d = decide_retry(_job(), ExitClassification.NORMAL_COMPLETION, None, _config())
    assert d.should_retry is False


def test_no_retry_on_user_interrupt():
    d = decide_retry(_job(), ExitClassification.USER_INTERRUPT, None, _config())
    assert d.should_retry is False


def test_rate_limit_with_reset_time():
    now = datetime.datetime.now(datetime.timezone.utc)
    reset = now + datetime.timedelta(hours=1)
    rl = _rl(reset_at=reset)
    d = decide_retry(_job(), ExitClassification.RATE_LIMIT, rl, _config())
    assert d.should_retry is True
    assert d.delay_seconds is not None
    assert 3500 <= d.delay_seconds <= 3700


def test_rate_limit_fallback_first():
    # No reset time: fallback = 30min * 2^0 = 1800s
    d = decide_retry(_job(rate_limit_retries=0), ExitClassification.RATE_LIMIT, _rl(), _config())
    assert d.should_retry is True
    assert d.delay_seconds == 1800.0


def test_rate_limit_fallback_backoff():
    # rate_limit_retries=1: 30min * 2^1 = 3600s
    d = decide_retry(_job(rate_limit_retries=1), ExitClassification.RATE_LIMIT, _rl(), _config())
    assert d.should_retry is True
    assert d.delay_seconds == 3600.0


def test_rate_limit_fallback_capped():
    # rate_limit_retries=20: would be huge, capped at 4h = 14400s
    d = decide_retry(_job(rate_limit_retries=20), ExitClassification.RATE_LIMIT, _rl(), _config())
    assert d.should_retry is True
    assert d.delay_seconds == 4 * 3600


def test_transient_error_first_retry():
    d = decide_retry(_job(retry_count=0), ExitClassification.TRANSIENT_ERROR, None, _config())
    assert d.should_retry is True
    assert d.delay_seconds == 30.0  # base * 2^0


def test_transient_error_backoff():
    d = decide_retry(_job(retry_count=2), ExitClassification.TRANSIENT_ERROR, None, _config())
    assert d.should_retry is True
    assert d.delay_seconds == 120.0  # 30 * 2^2


def test_transient_error_max_retries():
    d = decide_retry(_job(retry_count=5), ExitClassification.TRANSIENT_ERROR, None, _config())
    assert d.should_retry is False


def test_unknown_failure_first_retry():
    d = decide_retry(_job(retry_count=0), ExitClassification.UNKNOWN_FAILURE, None, _config())
    assert d.should_retry is True


def test_unknown_failure_max_retries():
    d = decide_retry(_job(retry_count=3), ExitClassification.UNKNOWN_FAILURE, None, _config())
    assert d.should_retry is False
