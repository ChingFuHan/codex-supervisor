import datetime
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
from codex_supervisor.models import Job, JobStatus
from codex_supervisor.scheduler import SystemdScheduler, SleepScheduler, get_scheduler


def _job(job_id="sv-20260906-120000") -> Job:
    return Job(
        job_id=job_id,
        status=JobStatus.SCHEDULED,
        codex_command=["codex", "exec", "--json", "test"],
        work_dir="/tmp/test-work",
        created_at="2026-09-06T12:00:00Z",
    )


def _future() -> datetime.datetime:
    return datetime.datetime(2099, 1, 1, 9, 0, 0, tzinfo=datetime.timezone.utc)


class TestSystemdScheduler:
    def test_service_unit_content(self, tmp_path):
        sched = SystemdScheduler(unit_dir=tmp_path)
        job = _job()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            sched.schedule_resume(job, _future())

        svc = (tmp_path / f"codex-supervisor-{job.job_id}.service").read_text()
        assert "codex-supervisor" in svc
        assert job.job_id in svc
        assert "Type=oneshot" in svc
        assert "resume" in svc

    def test_timer_unit_content(self, tmp_path):
        sched = SystemdScheduler(unit_dir=tmp_path)
        job = _job()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            sched.schedule_resume(job, _future())

        tmr = (tmp_path / f"codex-supervisor-{job.job_id}.timer").read_text()
        assert "OnCalendar=" in tmr
        assert "2099" in tmr
        assert "WantedBy=timers.target" in tmr

    def test_path_includes_nvm_dir(self, tmp_path):
        sched = SystemdScheduler(unit_dir=tmp_path)
        job = _job()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            sched.schedule_resume(job, _future())

        svc = (tmp_path / f"codex-supervisor-{job.job_id}.service").read_text()
        # PATH line should contain at least one /bin dir
        assert "Environment=PATH=" in svc
        path_line = [l for l in svc.splitlines() if l.startswith("Environment=PATH=")][0]
        assert "/bin" in path_line

    def test_cancel_removes_files(self, tmp_path):
        sched = SystemdScheduler(unit_dir=tmp_path)
        job = _job()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            sched.schedule_resume(job, _future())

        assert (tmp_path / f"codex-supervisor-{job.job_id}.service").exists()
        assert (tmp_path / f"codex-supervisor-{job.job_id}.timer").exists()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            sched.cancel_scheduled(job)

        assert not (tmp_path / f"codex-supervisor-{job.job_id}.service").exists()
        assert not (tmp_path / f"codex-supervisor-{job.job_id}.timer").exists()

    def test_systemctl_called_on_schedule(self, tmp_path):
        sched = SystemdScheduler(unit_dir=tmp_path)
        job = _job()

        calls_made = []
        def fake_run(cmd, **kwargs):
            calls_made.append(cmd)
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_run):
            sched.schedule_resume(job, _future())

        cmds = [" ".join(c) for c in calls_made]
        assert any("daemon-reload" in c for c in cmds)
        assert any("enable" in c for c in cmds)


class TestGetScheduler:
    def test_returns_systemd_when_available(self, tmp_path):
        with patch.object(SystemdScheduler, "is_available", return_value=True):
            s = get_scheduler(unit_dir=tmp_path)
            assert isinstance(s, SystemdScheduler)

    def test_returns_sleep_when_systemd_unavailable(self, tmp_path):
        with patch.object(SystemdScheduler, "is_available", return_value=False):
            s = get_scheduler(unit_dir=tmp_path)
            assert isinstance(s, SleepScheduler)
