"""
Core orchestrator: spawn Codex, monitor JSONL stdout, classify exit, wait, resume.
Runs as an iterative loop — no recursion, no stack growth.
"""

import datetime
import logging
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .interactive import InteractiveSupervisor
from .models import (
    ExitClassification,
    Job,
    JobStatus,
    RateLimitEvent,
    SupervisorConfig,
)
from .parser import classify_exit, detect_rate_limit, detect_rate_limit_stderr, extract_session_id, parse_event
from .retry import decide_retry
from .state import StateStore

logger = logging.getLogger(__name__)


def _make_job_id() -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"sv-{ts}-{uuid.uuid4().hex[:8]}"


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _find_codex(config: SupervisorConfig) -> str:
    if config.codex_path:
        return config.codex_path
    found = shutil.which("codex")
    if not found:
        raise FileNotFoundError("codex not found in PATH")
    return found


class CodexSupervisor:
    def __init__(
        self,
        config: SupervisorConfig,
        store: StateStore,
    ) -> None:
        self._config = config
        self._store = store
        self._proc: subprocess.Popen | None = None
        self._interrupted = False

    def run(self, codex_args: list[str], work_dir: str | None = None) -> int:
        """Start a new supervised Codex job. Loops until terminal state."""
        job_id = _make_job_id()
        work_dir = work_dir or str(Path.cwd())

        job = Job(
            job_id=job_id,
            status=JobStatus.RUNNING,
            codex_command=codex_args,
            work_dir=work_dir,
            created_at=_utcnow_iso(),
            last_start=_utcnow_iso(),
        )
        self._store.save_job(job)
        logger.info("job %s started: %s", job_id, codex_args)
        print(f"[codex-supervisor] job {job_id} started", file=sys.stderr)

        return self._run_loop(job, codex_args)

    def resume(self, job_id: str) -> int:
        """Resume a rate-limited job. Loops until terminal state."""
        job = self._store.load_job(job_id)
        if job is None:
            logger.error("job %s not found", job_id)
            return 1

        if job.status == JobStatus.CANCELLED:
            logger.info("job %s is cancelled, not resuming", job_id)
            return 0

        if job.session_id is None:
            logger.error("job %s has no session_id, cannot resume", job_id)
            return 1

        if job.mode == "interactive":
            return self.adopt(job.session_id, job.resume_prompt)

        args = self._resume_args(job)
        job.status = JobStatus.RUNNING
        job.last_start = _utcnow_iso()
        self._store.save_job(job)
        logger.info("job %s resuming session %s", job_id, job.session_id)
        print(f"[codex-supervisor] job {job_id} resuming session {job.session_id}", file=sys.stderr)

        return self._run_loop(job, args)

    def adopt(self, session_id: str, prompt: str = "continue", *,
              interval: int = 5, continue_now: bool = False) -> int:
        return InteractiveSupervisor(self._config, self._store, _find_codex(self._config)).watch(
            session_id, interval, prompt, continue_now,
        )

    def watch(self, poll_interval: int = 5, prompt: str = "continue") -> int:
        return InteractiveSupervisor(self._config, self._store, _find_codex(self._config)).watch(
            interval=poll_interval, prompt=prompt,
        )

    def _resume_args(self, job: Job) -> list[str]:
        return [
            _find_codex(self._config),
            "exec", "resume", "--json", job.session_id, job.resume_prompt,
        ]

    def _run_loop(self, job: Job, initial_args: list[str]) -> int:
        """Iterative loop: execute codex, handle exit, wait, resume, repeat."""
        args = initial_args

        while True:
            rc = self._run_once(job, args)

            decision = decide_retry(job, self._last_classification, self._last_rate_limit, self._config)
            logger.info("job %s: retry decision: %s", job.job_id, decision.reason)

            if not decision.should_retry:
                return self._finalize(job, self._last_classification, decision)

            wait_rc = self._wait_and_prepare(job, self._last_classification, decision)
            if wait_rc is not None:
                return wait_rc

            if job.session_id is None:
                logger.error("job %s has no session_id, cannot resume", job.job_id)
                job.status = JobStatus.FAILED
                job.last_error = "no session_id for resume"
                self._store.save_job(job)
                return 1

            args = self._resume_args(job)
            job.status = JobStatus.RUNNING
            job.last_start = _utcnow_iso()
            self._store.save_job(job)
            print(f"[codex-supervisor] job {job.job_id} resuming session {job.session_id}", file=sys.stderr)

    def _run_once(self, job: Job, args: list[str]) -> int:
        """Execute codex once, store classification. Returns exit code."""
        self._interrupted = False
        self._proc = None
        self._last_classification = ExitClassification.UNKNOWN_FAILURE
        self._last_rate_limit = None

        def _on_signal(signum, frame):
            self._interrupted = True
            logger.info("signal %d received for job %s", signum, job.job_id)
            if self._proc and self._proc.poll() is None:
                self._proc.send_signal(signum)

        old_sigint = signal.signal(signal.SIGINT, _on_signal)
        old_sigterm = signal.signal(signal.SIGTERM, _on_signal)

        try:
            return self._execute(job, args)
        finally:
            signal.signal(signal.SIGINT, old_sigint)
            signal.signal(signal.SIGTERM, old_sigterm)

    def _execute(self, job: Job, args: list[str]) -> int:
        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=job.work_dir,
                text=True,
                bufsize=1,
            )
        except (FileNotFoundError, OSError) as exc:
            logger.error("failed to spawn %s: %s", args[0], exc)
            job.status = JobStatus.FAILED
            job.last_error = str(exc)
            job.last_exit = _utcnow_iso()
            self._store.save_job(job)
            return 1

        stderr_lines: list[str] = []

        def _read_stderr():
            for line in self._proc.stderr:
                stderr_lines.append(line)
                sys.stderr.write(line)
                sys.stderr.flush()

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        events: list[dict] = []
        rate_limit_event: RateLimitEvent | None = None

        for raw_line in self._proc.stdout:
            sys.stdout.write(raw_line)
            sys.stdout.flush()

            event = parse_event(raw_line)
            if event is None:
                continue

            events.append(event)

            if job.session_id is None:
                session_id = extract_session_id(event)
                if session_id:
                    job.session_id = session_id
                    self._store.save_job(job)
                    logger.info("job %s: session_id = %s", job.job_id, session_id)

            if rate_limit_event is None:
                rl = detect_rate_limit(event)
                if rl and rl.detected:
                    rate_limit_event = rl
                    logger.info(
                        "job %s: rate limit detected (confidence=%.1f): %s",
                        job.job_id, rl.confidence, rl.raw_message[:100],
                    )

        self._proc.wait()
        stderr_thread.join(timeout=5)
        exit_code = self._proc.returncode
        self._proc = None

        job.last_exit = _utcnow_iso()
        stderr_text = "".join(stderr_lines)

        if not self._interrupted and "already has an active writer" in stderr_text:
            logger.warning(
                "job %s: session %s has an active writer lock conflict — "
                "a stale codex process may be holding the lock",
                job.job_id, job.session_id,
            )
            print(
                f"[codex-supervisor] lock conflict: session {job.session_id} has an active writer. "
                f"For an open interactive session, use adopt instead of exec resume.",
                file=sys.stderr,
            )
            self._last_classification = ExitClassification.TRANSIENT_ERROR
            self._last_rate_limit = None
            return exit_code

        if rate_limit_event is None:
            rl_stderr = detect_rate_limit_stderr(stderr_text)
            if rl_stderr and rl_stderr.detected:
                rate_limit_event = rl_stderr

        classification = classify_exit(events, exit_code, self._interrupted)
        if not self._interrupted and rate_limit_event and rate_limit_event.detected:
            classification = ExitClassification.RATE_LIMIT

        job.exit_classification = classification.value
        if rate_limit_event and rate_limit_event.detected:
            job.rate_limit_detected = _utcnow_iso()
            if rate_limit_event.reset_at:
                job.parsed_reset = rate_limit_event.reset_at.isoformat()

        logger.info("job %s: exit_code=%d classification=%s", job.job_id, exit_code, classification.value)

        self._last_classification = classification
        self._last_rate_limit = rate_limit_event
        return exit_code

    def _finalize(self, job: Job, classification: ExitClassification, decision) -> int:
        if classification == ExitClassification.NORMAL_COMPLETION:
            job.status = JobStatus.COMPLETED
            print(f"[codex-supervisor] job {job.job_id} completed", file=sys.stderr)
        elif classification == ExitClassification.USER_INTERRUPT:
            job.status = JobStatus.CANCELLED
            print(f"[codex-supervisor] job {job.job_id} cancelled (user interrupt)", file=sys.stderr)
        else:
            job.status = JobStatus.FAILED
            print(f"[codex-supervisor] job {job.job_id} failed ({decision.reason})", file=sys.stderr)
        self._store.save_job(job)
        return 0 if classification in (
            ExitClassification.NORMAL_COMPLETION,
            ExitClassification.USER_INTERRUPT,
        ) else 1

    def _wait_and_prepare(self, job, classification, decision) -> int | None:
        """Sleep until resume time. Returns exit code if cancelled, None to continue."""
        now = datetime.datetime.now(datetime.timezone.utc)
        delay = max(decision.delay_seconds or 0, 0)
        resume_at = now + datetime.timedelta(seconds=delay)

        if classification == ExitClassification.RATE_LIMIT:
            job.status = JobStatus.RATE_LIMITED
            job.rate_limit_retries += 1
        else:
            job.retry_count += 1

        job.scheduled_resume = resume_at.isoformat()
        self._store.save_job(job)

        print(
            f"[codex-supervisor] job {job.job_id} waiting {delay:.0f}s until {resume_at.isoformat()}",
            file=sys.stderr,
        )

        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            job.status = JobStatus.CANCELLED
            self._store.save_job(job)
            print(f"[codex-supervisor] job {job.job_id} cancelled during wait", file=sys.stderr)
            return 0

        return None
