"""
Real Codex smoke test.
Marked @pytest.mark.smoke — requires codex to be installed and quota available.
Run with: pytest tests/test_smoke.py -m smoke -v

Creates a temporary target directory, runs a tiny codex task,
verifies supervisor captures output and marks job COMPLETED.
Does NOT intentionally trigger rate limits.
"""

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from codex_supervisor.models import JobStatus, SupervisorConfig
from codex_supervisor.state import StateStore
from codex_supervisor.supervisor import CodexSupervisor


@pytest.mark.smoke
def test_real_codex_tiny_task(tmp_path):
    """
    Run a very small codex exec task in an ephemeral temp directory.
    Verify supervisor:
    - starts codex
    - captures JSONL output
    - sets session_id from thread.started event
    - marks job COMPLETED on exit 0
    """
    codex = shutil.which("codex")
    if not codex:
        pytest.skip("codex not found in PATH")

    target_dir = tmp_path / "target"
    target_dir.mkdir()

    codex_args = [
        codex, "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C", str(target_dir),
        "Print the text 'supervisor-smoke-test-ok' and nothing else.",
    ]

    config = SupervisorConfig(state_dir=tmp_path)
    store = StateStore(tmp_path)
    sup = CodexSupervisor(config, store)

    # Mock time.sleep so rate-limited runs don't block the test
    def fake_sleep(seconds):
        raise KeyboardInterrupt()

    with patch("codex_supervisor.supervisor.time.sleep", side_effect=fake_sleep):
        rc = sup.run(codex_args, work_dir=str(target_dir))

    jobs = store.load_all_jobs()
    assert len(jobs) == 1, f"expected 1 job, got {len(jobs)}"
    job = jobs[0]

    print(f"\n[smoke] job_id={job.job_id}")
    print(f"[smoke] session_id={job.session_id}")
    print(f"[smoke] status={job.status}")
    print(f"[smoke] exit_classification={job.exit_classification}")

    assert job.session_id is not None, "session_id not captured (thread.started event missing?)"

    # If rate-limited, supervisor would try to sleep but our mock raises KeyboardInterrupt
    # which marks job CANCELLED — that's fine for smoke test
    if job.status == JobStatus.CANCELLED and job.exit_classification == "rate_limit":
        print("[smoke] RATE_LIMIT detected — supervisor correctly classified")
        print(f"[smoke] parsed_reset={job.parsed_reset}")
        return

    assert job.status == JobStatus.COMPLETED, (
        f"expected COMPLETED (or CANCELLED due to rate limit), got {job.status.value} "
        f"(classification={job.exit_classification}, error={job.last_error})"
    )
