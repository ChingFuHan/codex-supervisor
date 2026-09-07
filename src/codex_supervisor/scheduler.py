"""
Legacy scheduler module. Default mechanism is now in-process time.sleep in supervisor.py.
"""

import abc
import datetime
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .models import Job

logger = logging.getLogger(__name__)

_SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"

_SERVICE_TEMPLATE = """\
[Unit]
Description=codex-supervisor resume {job_id}

[Service]
Type=oneshot
ExecStart={python} -m codex_supervisor resume {job_id}
Environment=PATH={path}
Environment=HOME={home}
WorkingDirectory={work_dir}
StandardOutput=append:{log_path}
StandardError=append:{log_path}
"""

_TIMER_TEMPLATE = """\
[Unit]
Description=codex-supervisor wake for {job_id}

[Timer]
OnCalendar={on_calendar}
Persistent=false
Unit=codex-supervisor-{job_id}.service

[Install]
WantedBy=timers.target
"""


def _build_path() -> str:
    dirs = set()
    for cmd in ("codex", "python3"):
        found = shutil.which(cmd)
        if found:
            dirs.add(str(Path(found).parent))
    dirs.update(["/usr/local/bin", "/usr/bin", "/bin"])
    # Preserve current PATH prefixes too
    for p in os.environ.get("PATH", "").split(":"):
        if p:
            dirs.add(p)
    # Keep order: nvm/local bins first
    ordered = []
    for p in os.environ.get("PATH", "").split(":"):
        if p in dirs:
            ordered.append(p)
            dirs.discard(p)
    ordered.extend(sorted(dirs))
    return ":".join(ordered)


class Scheduler(abc.ABC):
    @abc.abstractmethod
    def schedule_resume(self, job: Job, resume_at: datetime.datetime) -> None: ...

    @abc.abstractmethod
    def cancel_scheduled(self, job: Job) -> None: ...

    @abc.abstractmethod
    def list_scheduled(self) -> list[tuple[str, str]]: ...


class SystemdScheduler(Scheduler):
    def __init__(self, unit_dir: Path = _SYSTEMD_USER_DIR) -> None:
        self._unit_dir = unit_dir
        self._unit_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def is_available() -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _unit_name(self, job_id: str) -> str:
        return f"codex-supervisor-{job_id}"

    def _service_path(self, job_id: str) -> Path:
        return self._unit_dir / f"{self._unit_name(job_id)}.service"

    def _timer_path(self, job_id: str) -> Path:
        return self._unit_dir / f"{self._unit_name(job_id)}.timer"

    def schedule_resume(self, job: Job, resume_at: datetime.datetime) -> None:
        # Convert to local time for OnCalendar
        local_dt = resume_at.astimezone()
        on_calendar = local_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Determine log path
        from .state import StateStore
        from .config import load_config
        cfg = load_config()
        log_path = StateStore(cfg.state_dir).log_path(job.job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        python = sys.executable
        path = _build_path()
        home = str(Path.home())

        service_content = _SERVICE_TEMPLATE.format(
            job_id=job.job_id,
            python=python,
            path=path,
            home=home,
            work_dir=job.work_dir,
            log_path=log_path,
        )
        timer_content = _TIMER_TEMPLATE.format(
            job_id=job.job_id,
            on_calendar=on_calendar,
        )

        svc_path = self._service_path(job.job_id)
        tmr_path = self._timer_path(job.job_id)
        svc_path.write_text(service_content)
        tmr_path.write_text(timer_content)

        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=10)
            subprocess.run(
                ["systemctl", "--user", "enable", "--now",
                 f"{self._unit_name(job.job_id)}.timer"],
                check=True,
                timeout=10,
            )
            logger.info("systemd timer scheduled for %s at %s", job.job_id, on_calendar)
        except subprocess.CalledProcessError as exc:
            logger.error("systemctl failed: %s", exc)
            raise

    def cancel_scheduled(self, job: Job) -> None:
        unit = f"{self._unit_name(job.job_id)}.timer"
        for cmd in (
            ["systemctl", "--user", "stop", unit],
            ["systemctl", "--user", "disable", unit],
        ):
            subprocess.run(cmd, capture_output=True, timeout=10)

        for path in (self._service_path(job.job_id), self._timer_path(job.job_id)):
            path.unlink(missing_ok=True)

        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            timeout=10,
        )

    def list_scheduled(self) -> list[tuple[str, str]]:
        result = subprocess.run(
            ["systemctl", "--user", "list-timers", "codex-supervisor-*",
             "--no-legend", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        timers = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts:
                timers.append((parts[-1], " ".join(parts[:4])))
        return timers


class SleepScheduler(Scheduler):
    """Fallback: in-process sleep then call resume directly."""

    def __init__(self, supervisor_factory=None) -> None:
        self._supervisor_factory = supervisor_factory

    def schedule_resume(self, job: Job, resume_at: datetime.datetime) -> None:
        import time
        now = datetime.datetime.now(datetime.timezone.utc)
        delay = max((resume_at - now).total_seconds(), 0.0)
        logger.info(
            "SleepScheduler: sleeping %.0fs until %s for job %s",
            delay,
            resume_at.isoformat(),
            job.job_id,
        )
        time.sleep(delay)
        if self._supervisor_factory:
            sup = self._supervisor_factory()
            sup.resume(job.job_id)

    def cancel_scheduled(self, job: Job) -> None:
        pass  # In-process sleep cannot be cancelled externally

    def list_scheduled(self) -> list[tuple[str, str]]:
        return []


def get_scheduler(unit_dir: Path = _SYSTEMD_USER_DIR) -> Scheduler:
    if SystemdScheduler.is_available():
        return SystemdScheduler(unit_dir=unit_dir)
    logger.warning("systemd user session unavailable, using SleepScheduler fallback")
    return SleepScheduler()
