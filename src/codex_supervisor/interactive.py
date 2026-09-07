"""Observe an existing TUI and enqueue continuation without owning its process."""

import datetime as dt
import hashlib
import json
import logging
import signal
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from .codex_db import find_interactive_sessions, get_session_info, has_active_writer
from .models import ExitClassification, Job, JobStatus, SupervisorConfig
from .parser import detect_rate_limit, parse_event, parse_reset_time
from .retry import decide_retry
from .state import StateStore

logger = logging.getLogger(__name__)
UTC = dt.timezone.utc
# An accepted queue command is not proof of execution. Never resend on timeout.
QUEUE_ACK_SECONDS = 120


class RolloutTail:
    """Bounded-memory, incremental read of complete lifecycle records only."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.identity = None
        self.latest: dict | None = None

    def poll(self) -> dict | None:
        with self.path.open("rb") as stream:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity != self.identity or stat.st_size < self.offset:
                self.offset = 0
                self.latest = None
                self.identity = identity
            stream.seek(self.offset)
            while True:
                # Bound individual lines as well as the number retained. Oversize
                # non-lifecycle records are skipped without accumulating their body.
                line = stream.readline(1024 * 1024)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    if len(line) < 1024 * 1024:
                        break  # Writer has not completed this record yet.
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(1024 * 1024)
                    if not line:
                        break
                    self.offset = stream.tell()
                    continue
                self.offset = stream.tell()
                event = parse_event(line.decode("utf-8", errors="replace"))
                if not event or event.get("type") != "event_msg":
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") in ("task_started", "task_complete", "turn_aborted"):
                    self.latest = event
        return self.latest


def event_key(event: dict | None) -> str:
    if event is None:
        return "no-turn"
    payload = event["payload"]
    # No assistant/user text is retained in the job state or supervisor log.
    identity = [event.get("timestamp"), payload.get("turn_id"), payload.get("type")]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


class InteractiveSupervisor:
    def __init__(self, config: SupervisorConfig, store: StateStore, codex: str):
        self.config = config
        self.store = store
        self.codex = codex
        self.tails: dict[str, RolloutTail] = {}
        self.stopped = False

    @staticmethod
    def job_id(session_id: str) -> str:
        return "sv-interactive-" + str(uuid.UUID(session_id))

    def _record(self, job: Job, event: str, now: dt.datetime, **details):
        record = dict(timestamp=now.isoformat(), job=job.job_id, session=job.session_id,
                      event=event, status=job.status.value, **details)
        with self.store.log_path(job.job_id).open("a") as stream:
            stream.write(json.dumps(record) + "\n")
        logger.info("job=%s event=%s %s", job.job_id, event, details)

    def _last_log_event(self, job: Job) -> str | None:
        path = self.store.log_path(job.job_id)
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value.get("event")
        return None

    def _reactivate_after_monitor_stop(self, job: Job, now: dt.datetime) -> bool:
        """Recover jobs cancelled by the old watcher shutdown handler once."""
        if job.status != JobStatus.CANCELLED:
            return False
        if self._last_log_event(job) != "MONITOR_STOPPED":
            return False
        job.status = JobStatus.RUNNING
        job.last_error = None
        job.observed_event = None
        self._record(job, "MONITOR_REATTACHED", now)
        self.store.save_job(job)
        return True

    def _snapshot(self, info: dict) -> dict | None:
        sid = info["id"]
        path = Path(info["rollout_path"])
        tail = self.tails.get(sid)
        if tail is None or tail.path != path:
            tail = self.tails[sid] = RolloutTail(path)
        return tail.poll()

    def tick(self, info: dict, prompt: str = "continue", *, adopt: bool = False,
             continue_now: bool = False, now: dt.datetime | None = None) -> Job | None:
        """One non-sleeping session step; the file lock also protects other watchers."""
        now = now or dt.datetime.now(UTC)
        sid = str(uuid.UUID(info["id"]))
        jid = self.job_id(sid)
        with self.store.lock_job(jid):
            event = self._snapshot(info)
            key = event_key(event)
            payload = event["payload"] if event else {}
            kind = payload.get("type")
            limit = detect_rate_limit(event) if event and kind == "task_complete" else None
            job = self.store.load_job(jid, strict=True)
            if job is None:
                if not adopt and not limit:
                    return None
                job = Job(jid, JobStatus.RUNNING,
                          [self.codex, "queue", "--thread", sid, "--message", prompt],
                          info["cwd"], now.isoformat(), session_id=sid,
                          resume_prompt=prompt, mode="interactive")
                self.store.save_job(job)
                self._record(job, "MONITORING", now)
            if job.mode != "interactive":
                raise ValueError("job is not an interactive monitor")
            if adopt:
                job.resume_prompt = prompt
                if job.status == JobStatus.CANCELLED:
                    job.status = JobStatus.RUNNING
                    if not job.queued_at:
                        job.observed_event = None
                self.store.save_job(job)
            elif job.status == JobStatus.CANCELLED:
                self._reactivate_after_monitor_stop(job, now)
            if job.status == JobStatus.CANCELLED:
                return job

            if continue_now:
                if job.queued_at:
                    raise ValueError("a continuation is already pending; not enqueueing another")
                # Explicit operator action, including an idle session with no error.
                job.observed_event = key
                job.scheduled_resume = None
                self._enqueue(job, key, now)
                return job

            if job.queued_at:
                if key != job.observed_event:
                    job.queued_at = None
                    self._record(job, "NEW_LIFECYCLE_EVENT", now, lifecycle=kind)
                elif (now - dt.datetime.fromisoformat(job.queued_at)).total_seconds() >= QUEUE_ACK_SECONDS:
                    if job.status != JobStatus.FAILED:
                        job.status = JobStatus.FAILED
                        job.last_error = "queue delivery not confirmed within 120s; not resending; check the original TUI"
                        self.store.save_job(job)
                        self._record(job, "QUEUE_UNCONFIRMED", now)
                    return job
                else:
                    return job

            if key != job.observed_event:
                job.observed_event = key
                job.scheduled_resume = None
                job.parsed_reset = None
                job.last_error = None
                if kind == "task_started":
                    job.status = JobStatus.RUNNING
                    job.last_start = event.get("timestamp", now.isoformat())
                    job.exit_classification = None
                    self._record(job, "TURN_STARTED", now)
                elif kind == "turn_aborted":
                    job.status = JobStatus.CANCELLED
                    job.exit_classification = ExitClassification.USER_INTERRUPT.value
                    self._record(job, "USER_INTERRUPT", now)
                elif kind == "task_complete":
                    job.last_exit = event.get("timestamp", now.isoformat())
                    if limit:
                        # Old reset times stay in the past; never move them to tomorrow
                        # just because this is a new observer of an old error.
                        try:
                            reference = dt.datetime.fromisoformat(event["timestamp"])
                        except (KeyError, ValueError, TypeError):
                            reference = now
                        limit.reset_at = parse_reset_time(limit.raw_message, reference)
                        decision = decide_retry(job, ExitClassification.RATE_LIMIT, limit, self.config)
                        wake = limit.reset_at or now + dt.timedelta(seconds=decision.delay_seconds)
                        job.status = JobStatus.RATE_LIMITED
                        job.exit_classification = ExitClassification.RATE_LIMIT.value
                        job.rate_limit_detected = now.isoformat()
                        job.parsed_reset = limit.reset_at.isoformat() if limit.reset_at else None
                        job.scheduled_resume = wake.isoformat()
                        self._record(job, "RATE_LIMIT", now, reset_at=job.parsed_reset,
                                     wake_at=job.scheduled_resume, retry=job.rate_limit_retries)
                    elif payload.get("error"):
                        job.status = JobStatus.FAILED
                        job.exit_classification = ExitClassification.UNKNOWN_FAILURE.value
                        job.last_error = "Codex turn failed without a rate-limit error; inspect original TUI"
                        self._record(job, "TURN_FAILED", now)
                    else:
                        job.status = JobStatus.COMPLETED
                        job.exit_classification = ExitClassification.NORMAL_COMPLETION.value
                        job.rate_limit_retries = 0
                        self._record(job, "TURN_COMPLETED", now)
                self.store.save_job(job)

            if (job.scheduled_resume and job.status == JobStatus.RATE_LIMITED
                    and now >= dt.datetime.fromisoformat(job.scheduled_resume)
                    and job.submitted_event != key):
                # Recheck right before submitting: a manual continuation or interrupt
                # observed during the wait supersedes this scheduled retry.
                if event_key(self._snapshot(info)) == key:
                    self._enqueue(job, key, now)
            return job

    def _enqueue(self, job: Job, key: str, now: dt.datetime):
        if self.stopped:
            return
        if not has_active_writer(job.session_id):
            raise ValueError("session has no live Codex writer; open its TUI first; no message was queued")
        # Durable intent BEFORE the external call gives at-most-once submission for
        # an error event, even if the watcher crashes after queue accepted the input.
        job.submitted_event = key
        job.queued_at = now.isoformat()
        job.status = JobStatus.SCHEDULED
        job.scheduled_resume = None
        job.last_error = None
        job.queue_id = None
        job.rate_limit_retries += 1
        job.codex_command = [self.codex, "queue", "--thread", job.session_id,
                             "--message", job.resume_prompt]
        self.store.save_job(job)
        self._record(job, "QUEUE_INTENT", now, retry=job.rate_limit_retries)
        try:
            result = subprocess.run(job.codex_command, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(f"codex queue exited {result.returncode}: {result.stderr[-500:]}")
            # CLI 0.153.4: Queued message <UUID> for thread <UUID>.
            words = result.stdout.split()
            if len(words) >= 3 and words[:2] == ["Queued", "message"]:
                job.queue_id = str(uuid.UUID(words[2]))
            self._record(job, "QUEUED", now, queue_id=job.queue_id, exit_code=result.returncode)
        except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
            job.status = JobStatus.FAILED
            job.last_error = f"queue outcome unconfirmed; not resending: {exc}"
            self._record(job, "QUEUE_FAILED", now, error=job.last_error)
        self.store.save_job(job)

    def watch(self, session_id: str | None = None, interval: int = 5,
              prompt: str = "continue", continue_now: bool = False) -> int:
        if interval <= 0:
            raise ValueError("interval must be positive")
        if session_id:
            session_id = str(uuid.UUID(session_id))
        # Fail before attaching: older Codex versions must not silently use exec.
        capability = subprocess.run([self.codex, "queue", "--help"],
                                    capture_output=True, text=True, timeout=10)
        if capability.returncode or "--thread" not in capability.stdout:
            raise RuntimeError("this Codex installation does not support codex queue --thread")
        old_handlers = {}
        monitored = set()
        def stop(signum, frame):
            self.stopped = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, stop)
        first = True
        logger.info("interactive monitor started; original Codex TUI stays open; interval=%ss", interval)
        try:
            with self.store.watch_lock():
                return self._watch_loop(
                    session_id, interval, prompt, continue_now,
                    old_handlers, monitored,
                )
        except (OSError, sqlite3.Error) as exc:
            logger.error("interactive monitor stopped: %s", exc)
            return 1
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)

    def _watch_loop(self, session_id, interval, prompt, continue_now,
                    old_handlers, monitored):
        last_summary = None
        first = True
        try:
            while not self.stopped:
                infos = [get_session_info(session_id)] if session_id else find_interactive_sessions()
                if session_id and not infos[0]:
                    raise ValueError(f"session {session_id} not found")
                active_infos = [info for info in infos if info and has_active_writer(info["id"])]
                summary = (len(infos), len(active_infos), len(monitored))
                if summary != last_summary:
                    logger.info(
                        "watch summary: discovered=%d active=%d tracked=%d interval=%ss",
                        summary[0], summary[1], summary[2], interval,
                    )
                    last_summary = summary
                for info in infos:
                    if self.stopped:
                        break
                    try:
                        if not info:
                            continue
                        if not has_active_writer(info["id"]):
                            if session_id:
                                raise ValueError("session has no live Codex writer; open its TUI first")
                            continue
                        job = self.tick(info, prompt, adopt=bool(session_id and first),
                                        continue_now=bool(session_id and first and continue_now))
                        if job:
                            monitored.add(job.job_id)
                        if session_id and job and job.status == JobStatus.CANCELLED:
                            return 0
                    except (OSError, ValueError) as exc:
                        logger.error("session=%s monitor error: %s", info["id"], exc)
                        if session_id:
                            return 1
                first = False
                # Fast Ctrl+C/SIGTERM response, no signals sent to Codex or shells.
                deadline = time.monotonic() + interval
                while not self.stopped and time.monotonic() < deadline:
                    time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        finally:
            if self.stopped:
                for jid in monitored:
                    with self.store.lock_job(jid):
                        job = self.store.load_job(jid)
                        if job and job.status != JobStatus.CANCELLED:
                            self._record(job, "MONITOR_STOPPED", dt.datetime.now(UTC))
        return 0
