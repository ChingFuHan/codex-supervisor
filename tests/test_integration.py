"""
End-to-end integration test: run -> rate limit -> wait -> resume -> complete.
Uses fake codex scripts. Supervisor now waits in-process (time.sleep mocked).
"""

import datetime
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from codex_supervisor.models import JobStatus, SupervisorConfig, ExitClassification
from codex_supervisor.retry import decide_retry
from codex_supervisor.state import StateStore
from codex_supervisor.supervisor import CodexSupervisor

FAKE_DIR = Path(__file__).parent / "fake_codex"


def _script(name: str) -> list[str]:
    return [sys.executable, str(FAKE_DIR / name)]


def test_normal_completion_lifecycle(tmp_path):
    config = SupervisorConfig(state_dir=tmp_path)
    store = StateStore(tmp_path)
    sup = CodexSupervisor(config, store)
    rc = sup.run(_script("normal_exit.py"))
    assert rc == 0
    job = store.load_all_jobs()[0]
    assert job.status == JobStatus.COMPLETED
    assert job.session_id is not None
    assert job.exit_classification == "normal_completion"


def test_rate_limit_waits_then_resumes(tmp_path):
    """
    Rate limit detected: supervisor sleeps, then calls resume().
    resume() will fail (fake session_id), but we verify:
    - sleep was called with correct delay
    - rate_limit_retries incremented
    - session_id and parsed_reset captured
    """
    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    config = SupervisorConfig(state_dir=tmp_path, fallback_wait_minutes=1)
    store = StateStore(tmp_path)
    sup = CodexSupervisor(config, store)

    with patch("codex_supervisor.supervisor.time.sleep", side_effect=fake_sleep):
        rc = sup.run(_script("rate_limit_with_time.py"))

    assert len(sleep_calls) >= 1
    assert sleep_calls[0] > 0  # first sleep for rate limit reset

    job = store.load_all_jobs()[0]
    assert job.session_id == "01a00000-0000-7000-0000-000000000002"
    assert job.rate_limit_retries >= 1
    assert job.rate_limit_detected is not None
    assert job.parsed_reset is not None


def test_transient_error_retry(tmp_path):
    """Transient error triggers in-process wait and resume attempt."""
    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    config = SupervisorConfig(state_dir=tmp_path, backoff_base_seconds=1)
    store = StateStore(tmp_path)
    sup = CodexSupervisor(config, store)

    with patch("codex_supervisor.supervisor.time.sleep", side_effect=fake_sleep):
        sup.run(_script("transient_failure.py"))

    assert len(sleep_calls) >= 1
    job = store.load_all_jobs()[0]
    assert job.retry_count >= 1
    assert job.exit_classification == "transient_error"


def test_max_retries_stops(tmp_path):
    """After max_crash_retries exhausted, decide_retry returns should_retry=False."""
    from codex_supervisor.models import Job
    import datetime

    config = SupervisorConfig(state_dir=tmp_path, max_crash_retries=2, backoff_base_seconds=1)

    job = Job(
        job_id="sv-test-maxretry",
        status=JobStatus.RUNNING,
        codex_command=["fake"],
        work_dir="/tmp",
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        retry_count=2,
    )
    decision = decide_retry(job, ExitClassification.TRANSIENT_ERROR, None, config)
    assert decision.should_retry is False
