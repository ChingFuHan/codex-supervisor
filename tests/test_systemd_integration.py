"""
systemd user timer integration test.
Marked @pytest.mark.slow — requires a running systemd user session.
Run with: pytest tests/test_systemd_integration.py -m slow

Creates a real timer that fires in 5 seconds, runs a fake job,
verifies state is updated, then cleans up.
"""

import os
import sys
import time
import datetime
from pathlib import Path

import pytest

from codex_supervisor.models import Job, JobStatus, SupervisorConfig
from codex_supervisor.scheduler import SystemdScheduler
from codex_supervisor.state import StateStore

FAKE_DIR = Path(__file__).parent / "fake_codex"


def _write_fake_codex_binary(bin_dir: Path) -> None:
    """
    Write a fake `codex` binary into bin_dir that emits normal_exit.py JSONL
    for any `exec resume --json <uuid>` invocation, and forwards to the real
    fake scripts for `exec --json` (initial run).
    """
    fake_codex = bin_dir / "codex"
    normal_exit_script = str(FAKE_DIR / "normal_exit.py")
    fake_codex.write_text(f"""\
#!/usr/bin/env python3
import sys
args = sys.argv[1:]
# Any invocation: just emit normal completion JSONL
import json
SESSION_ID = "01a00000-0000-7000-0000-000000000001"
def emit(obj):
    print(json.dumps(obj), flush=True)
emit({{"timestamp": "2026-09-06T00:00:00Z", "ordinal": 0, "type": "session_meta",
      "payload": {{"session_id": SESSION_ID, "cwd": "/tmp"}}}})
emit({{"timestamp": "2026-09-06T00:00:01Z", "ordinal": 1, "type": "event_msg",
      "payload": {{"type": "task_started", "turn_id": "turn-1"}}}})
emit({{"timestamp": "2026-09-06T00:00:02Z", "ordinal": 2, "type": "event_msg",
      "payload": {{"type": "task_complete", "turn_id": "turn-1", "error": None}}}})
sys.exit(0)
""")
    fake_codex.chmod(0o755)


@pytest.mark.slow
def test_systemd_timer_fires_and_resumes(tmp_path):
    """
    Schedule a fake job to resume in 5 seconds via real systemd timer.
    Uses a fake `codex` binary in a temp bin dir to avoid touching real quota.
    Verifies: timer fires, supervisor runs, job marked COMPLETED.
    """
    import subprocess

    if not SystemdScheduler.is_available():
        pytest.skip("systemd user session not available")

    # Create fake codex binary
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    _write_fake_codex_binary(fake_bin)

    config = SupervisorConfig(state_dir=tmp_path, codex_path=str(fake_bin / "codex"))
    store = StateStore(tmp_path)

    job_id = "sv-systemd-test-5s"
    session_id = "01a00000-0000-7000-0000-000000000001"

    job = Job(
        job_id=job_id,
        status=JobStatus.SCHEDULED,
        codex_command=[str(fake_bin / "codex"), "exec", "--json", "test"],
        work_dir=str(tmp_path),
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        session_id=session_id,
    )
    store.save_job(job)

    resume_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=5)
    local_dt = resume_at.astimezone()
    on_calendar = local_dt.strftime("%Y-%m-%d %H:%M:%S")

    src_dir = str(Path(__file__).parent.parent / "src")
    log_path = store.log_path(job_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    svc_path = unit_dir / f"codex-supervisor-{job_id}.service"
    tmr_path = unit_dir / f"codex-supervisor-{job_id}.timer"

    # PATH: put fake_bin first so our fake codex is found before real one
    service_path = str(fake_bin) + ":" + os.environ.get("PATH", "/usr/bin:/bin")

    svc_content = f"""\
[Unit]
Description=codex-supervisor resume {job_id} (test)

[Service]
Type=oneshot
ExecStart={sys.executable} -m codex_supervisor resume {job_id}
Environment=PATH={service_path}
Environment=HOME={Path.home()}
Environment=CODEX_SUPERVISOR_STATE_DIR={tmp_path}
Environment=CODEX_SUPERVISOR_CODEX_PATH={fake_bin / "codex"}
Environment=PYTHONPATH={src_dir}
WorkingDirectory={tmp_path}
StandardOutput=append:{log_path}
StandardError=append:{log_path}
"""

    tmr_content = f"""\
[Unit]
Description=codex-supervisor test wake for {job_id}

[Timer]
OnCalendar={on_calendar}
Persistent=false
Unit=codex-supervisor-{job_id}.service

[Install]
WantedBy=timers.target
"""

    svc_path.write_text(svc_content)
    tmr_path.write_text(tmr_content)

    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=10)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"codex-supervisor-{job_id}.timer"],
            check=True, timeout=10,
        )
        print(f"\n[test] timer scheduled for {on_calendar}, waiting up to 20s...")

        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(1)
            loaded = store.load_job(job_id)
            if loaded and loaded.status == JobStatus.COMPLETED:
                print(f"[test] job {job_id} completed!")
                break
        else:
            if log_path.exists():
                print(f"\n[test] log:\n{log_path.read_text()}")
            pytest.fail(f"job {job_id} did not complete within 20s")

    finally:
        for cmd in (
            ["systemctl", "--user", "stop", f"codex-supervisor-{job_id}.timer"],
            ["systemctl", "--user", "disable", f"codex-supervisor-{job_id}.timer"],
            ["systemctl", "--user", "stop", f"codex-supervisor-{job_id}.service"],
        ):
            subprocess.run(cmd, capture_output=True, timeout=10)
        svc_path.unlink(missing_ok=True)
        tmr_path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
        print(f"[test] cleanup done for {job_id}")
