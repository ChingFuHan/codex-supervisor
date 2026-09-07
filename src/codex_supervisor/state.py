import json
import logging
import fcntl
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .models import Job, JobStatus, SupervisorConfig

logger = logging.getLogger(__name__)


class WatchAlreadyRunning(RuntimeError):
    """Another supervisor process owns the global watch lock."""


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self._jobs_dir = state_dir / "jobs"
        self._logs_dir = state_dir / "logs"
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        self._logs_dir.mkdir(parents=True, exist_ok=True)

    def _job_path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
            raise ValueError("invalid job id")
        return self._jobs_dir / f"{job_id}.json"

    @contextmanager
    def lock_job(self, job_id: str):
        """Serialize queue decisions and cancellation across supervisor processes."""
        with self._job_path(job_id).with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def watch_lock(self):
        """Keep one global watcher per state directory."""
        path = self._jobs_dir.parent / "watch.lock"
        with path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WatchAlreadyRunning(
                    f"watch already running (lock: {path})"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def save_job(self, job: Job) -> None:
        path = self._job_path(job.job_id)
        fd, name = tempfile.mkstemp(dir=self._jobs_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(job.to_dict(), stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            directory_fd = os.open(self._jobs_dir, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(name).unlink(missing_ok=True)

    def load_job(self, job_id: str, *, strict: bool = False) -> Job | None:
        path = self._job_path(job_id)
        if not path.exists():
            return None
        try:
            return Job.from_dict(json.loads(path.read_text()))
        except (ValueError, KeyError, TypeError) as exc:
            if strict:
                raise ValueError(f"corrupt job file {path}; refusing to lose submission history") from exc
            logger.warning("corrupt job file %s: %s", path, exc)
            return None

    def load_all_jobs(self) -> list[Job]:
        jobs = []
        for path in sorted(self._jobs_dir.glob("*.json")):
            try:
                jobs.append(Job.from_dict(json.loads(path.read_text())))
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning("corrupt job file %s: %s", path, exc)
        return jobs

    def update_job(self, job_id: str, **updates) -> Job:
        job = self.load_job(job_id)
        if job is None:
            raise FileNotFoundError(f"job {job_id} not found")
        for k, v in updates.items():
            setattr(job, k, v)
        self.save_job(job)
        return job

    def log_path(self, job_id: str) -> Path:
        return self._logs_dir / f"{job_id}.log"
