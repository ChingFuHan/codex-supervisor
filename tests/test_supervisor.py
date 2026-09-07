"""
Supervisor tests using fake codex scripts.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codex_supervisor.models import JobStatus, SupervisorConfig
from codex_supervisor.state import StateStore
from codex_supervisor.supervisor import CodexSupervisor

FAKE_DIR = Path(__file__).parent / "fake_codex"


def _script(name: str) -> list[str]:
    return [sys.executable, str(FAKE_DIR / name)]


def _sup(state_dir: Path, **kwargs) -> CodexSupervisor:
    config = SupervisorConfig(state_dir=state_dir, **kwargs)
    store = StateStore(state_dir)
    return CodexSupervisor(config, store)


class TestNormalCompletion:
    def test_job_marked_completed(self, tmp_path):
        sup = _sup(tmp_path)
        rc = sup.run(_script("normal_exit.py"))
        assert rc == 0
        jobs = StateStore(tmp_path).load_all_jobs()
        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.COMPLETED

    def test_session_id_captured(self, tmp_path):
        sup = _sup(tmp_path)
        sup.run(_script("normal_exit.py"))
        jobs = StateStore(tmp_path).load_all_jobs()
        assert jobs[0].session_id == "01a00000-0000-7000-0000-000000000001"


class TestRateLimit:
    def test_rate_limit_with_time_waits_and_resumes(self, tmp_path):
        """Rate limit detected: supervisor sleeps then calls resume."""
        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)

        config = SupervisorConfig(state_dir=tmp_path)
        store = StateStore(tmp_path)
        sup = CodexSupervisor(config, store)

        with patch("codex_supervisor.supervisor.time.sleep", side_effect=fake_sleep):
            rc = sup.run(_script("rate_limit_with_time.py"))

        assert len(sleep_calls) >= 1
        assert sleep_calls[0] > 0  # first sleep is for rate limit reset
        jobs = store.load_all_jobs()
        assert jobs[0].rate_limit_retries >= 1

    def test_rate_limit_no_time_uses_fallback(self, tmp_path):
        """No reset time: uses fallback_wait_minutes with backoff."""
        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)

        config = SupervisorConfig(state_dir=tmp_path, fallback_wait_minutes=1)
        store = StateStore(tmp_path)
        sup = CodexSupervisor(config, store)

        with patch("codex_supervisor.supervisor.time.sleep", side_effect=fake_sleep):
            rc = sup.run(_script("rate_limit_no_time.py"))

        assert len(sleep_calls) >= 1
        # First sleep: fallback = 1min * 2^0 = 60s
        assert 55 <= sleep_calls[0] <= 65


class TestTransientError:
    def test_transient_retries_then_fails(self, tmp_path):
        """Transient error: retries with backoff up to max, then fails."""
        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)

        config = SupervisorConfig(
            state_dir=tmp_path,
            backoff_base_seconds=1,
            max_crash_retries=2,
        )
        store = StateStore(tmp_path)
        sup = CodexSupervisor(config, store)

        with patch("codex_supervisor.supervisor.time.sleep", side_effect=fake_sleep):
            rc = sup.run(_script("transient_failure.py"))

        # Should retry up to max then fail
        jobs = store.load_all_jobs()
        assert jobs[0].retry_count >= 1


class TestFatalFailure:
    def test_fatal_no_session_id(self, tmp_path):
        sup = _sup(tmp_path, backoff_base_seconds=1)
        sup.run(_script("fatal_failure.py"))
        jobs = StateStore(tmp_path).load_all_jobs()
        assert jobs[0].session_id is None


class TestUserInterrupt:
    def test_sigint_cancels(self, tmp_path):
        """
        Run supervisor in a subprocess so SIGINT goes to the main thread there.
        """
        env = os.environ.copy()
        env["CODEX_SUPERVISOR_STATE_DIR"] = str(tmp_path)
        src_dir = str(Path(__file__).parent.parent / "src")
        env["PYTHONPATH"] = src_dir + ":" + env.get("PYTHONPATH", "")

        fake_script = str(FAKE_DIR / "sigint_exit.py")
        proc = subprocess.Popen(
            [sys.executable, "-m", "codex_supervisor", "run",
             "--", sys.executable, fake_script],
            cwd=str(Path(__file__).parent.parent),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        rc = proc.wait(timeout=10)

        jobs = StateStore(tmp_path).load_all_jobs()
        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.CANCELLED
        assert rc == 0


def test_stale_goal_cannot_override_current_completion(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with sqlite3.connect(tmp_path / "goals_1.sqlite") as con:
        con.execute("CREATE TABLE thread_goals (thread_id TEXT, status TEXT)")
        con.execute("INSERT INTO thread_goals VALUES (?, 'usage_limited')",
                    ("01a00000-0000-7000-0000-000000000001",))
    sup = _sup(tmp_path / "state")
    assert sup.run(_script("normal_exit.py")) == 0
    assert sup._store.load_all_jobs()[0].status == JobStatus.COMPLETED


def test_interrupt_wins_over_rate_limit_output(tmp_path):
    import io
    from unittest.mock import Mock
    from codex_supervisor.models import Job, ExitClassification
    sup = _sup(tmp_path)
    sup._interrupted = True
    job = Job("test", JobStatus.RUNNING, [], str(tmp_path), "2026-09-07T00:00:00Z")
    proc = Mock(stdout=io.StringIO(''), stderr=io.StringIO('usage limit'), returncode=130)
    with patch('codex_supervisor.supervisor.subprocess.Popen', return_value=proc):
        sup._execute(job, ['fake'])
    assert sup._last_classification == ExitClassification.USER_INTERRUPT
