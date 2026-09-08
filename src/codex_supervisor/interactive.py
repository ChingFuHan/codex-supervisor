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
PROGRESS_ITEM_TYPES = frozenset(("Reasoning", "CommandExecution", "Extension", "AgentMessage"))


class RolloutTail:
    """Bounded-memory, incremental read of lifecycle and current-turn progress."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.identity = None
        self.latest: dict | None = None
        self.active_turn_id: str | None = None
        self.active_turn_started_at: str | None = None
        self.active_turn_terminal = False
        self.progress_item_count = 0
        self.progress_last_at: str | None = None
        self.progress_item_types: list[str] = []

    def poll(self) -> dict | None:
        with self.path.open("rb") as stream:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity != self.identity or stat.st_size < self.offset:
                self.offset = 0
                self.latest = None
                self.identity = identity
                self.active_turn_id = None
                self.active_turn_started_at = None
                self.active_turn_terminal = False
                self.progress_item_count = 0
                self.progress_last_at = None
                self.progress_item_types = []
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
                kind = payload.get("type")
                if kind in ("task_started", "task_complete", "turn_aborted"):
                    self.latest = event
                    if kind == "task_started":
                        self.active_turn_id = payload.get("turn_id")
                        self.active_turn_started_at = event.get("timestamp")
                        self.active_turn_terminal = False
                        self.progress_item_count = 0
                        self.progress_last_at = None
                        self.progress_item_types = []
                    elif payload.get("turn_id") == self.active_turn_id:
                        self.active_turn_terminal = True
                elif kind == "item_completed":
                    item = payload.get("item")
                    item_type = item.get("type") if isinstance(item, dict) else None
                    turn_id = payload.get("turn_id")
                    if (turn_id == self.active_turn_id and not self.active_turn_terminal
                            and item_type in PROGRESS_ITEM_TYPES):
                        self.progress_item_count += 1
                        self.progress_last_at = event.get("timestamp")
                        if item_type not in self.progress_item_types:
                            self.progress_item_types.append(item_type)
        return self.latest

    @property
    def progress_snapshot(self) -> dict:
        return {
            "turn_id": self.active_turn_id,
            "started_at": self.active_turn_started_at,
            "item_count": self.progress_item_count,
            "last_at": self.progress_last_at,
            "item_types": list(self.progress_item_types),
        }


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

    def _observe_progress(self, job: Job, progress: dict, now: dt.datetime) -> bool:
        """Persist only progress belonging to the continuation turn."""
        if (not job.continuation_turn_id
                or progress["turn_id"] != job.continuation_turn_id
                or job.continuation_outcome in {
                    "completed", "rate_limited_before_progress", "rate_limited_after_progress",
                    "failed", "aborted", "unconfirmed",
                }):
            return False
        if progress["item_count"] <= job.progress_item_count:
            return False
        job.progress_item_count = progress["item_count"]
        job.last_progress_at = progress["last_at"] or now.isoformat()
        job.progress_summary = progress["item_types"]
        if job.continuation_outcome in ("queued", "started"):
            job.continuation_outcome = "working"
            self._record(job, "CONTINUATION_WORKING", now,
                         turn_id=job.continuation_turn_id,
                         progress_items=job.progress_item_count,
                         progress_types=job.progress_summary)
        return True

    def _bind_continuation(self, job: Job, event: dict, tail: RolloutTail,
                           now: dt.datetime) -> None:
        payload = event.get("payload", {})
        turn_id = payload.get("turn_id")
        if not turn_id or job.continuation_turn_id:
            return
        job.continuation_turn_id = turn_id
        if tail.active_turn_id == turn_id:
            job.continuation_started_at = tail.active_turn_started_at
        elif payload.get("type") == "task_started":
            job.continuation_started_at = event.get("timestamp", now.isoformat())

    def _mark_continuation_terminal(self, job: Job, outcome: str,
                                    now: dt.datetime) -> None:
        if not job.continuation_turn_id and job.continuation_outcome != "queued":
            return
        job.continuation_outcome = outcome
        event = {
            "completed": "CONTINUATION_COMPLETED",
            "rate_limited_before_progress": "CONTINUATION_RATE_LIMITED_BEFORE_PROGRESS",
            "rate_limited_after_progress": "CONTINUATION_RATE_LIMITED_AFTER_PROGRESS",
            "failed": "CONTINUATION_FAILED",
            "aborted": "CONTINUATION_ABORTED",
        }.get(outcome)
        if event:
            self._record(job, event, now,
                         turn_id=job.continuation_turn_id,
                         progress_items=job.progress_item_count,
                         progress_types=job.progress_summary)

    def _restore_legacy_terminal(self, job: Job, event: dict, tail: RolloutTail,
                                 progress: dict, key: str, now: dt.datetime) -> bool:
        """Backfill outcome for a queued turn observed by the pre-progress watcher."""
        payload = event.get("payload", {})
        if (job.continuation_outcome is not None
                or job.status != JobStatus.RATE_LIMITED
                or not job.queue_id
                or payload.get("type") != "task_complete"
                or job.last_exit != event.get("timestamp")
                or not job.submitted_event
                or job.submitted_event == key):
            return False
        job.continuation_outcome = "queued"
        self._bind_continuation(job, event, tail, now)
        if progress["turn_id"] == job.continuation_turn_id:
            job.progress_item_count = progress["item_count"]
            job.last_progress_at = progress["last_at"]
            job.progress_summary = progress["item_types"]
        self._mark_continuation_terminal(
            job,
            "rate_limited_after_progress" if job.progress_item_count else
            "rate_limited_before_progress",
            now,
        )
        return True

    def tick(self, info: dict, prompt: str = "continue", *, adopt: bool = False,
             continue_now: bool = False, now: dt.datetime | None = None) -> Job | None:
        """One non-sleeping session step; the file lock also protects other watchers."""
        now = now or dt.datetime.now(UTC)
        sid = str(uuid.UUID(info["id"]))
        jid = self.job_id(sid)
        with self.store.lock_job(jid):
            event = self._snapshot(info)
            tail = self.tails[sid]
            progress = tail.progress_snapshot
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

            legacy_restored = self._restore_legacy_terminal(
                job, event, tail, progress, key, now,
            ) if event else False
            progress_changed = self._observe_progress(job, progress, now)
            if progress_changed or legacy_restored:
                self.store.save_job(job)

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
                    continuation_pending = True
                    job.queued_at = None
                    self._record(job, "NEW_LIFECYCLE_EVENT", now, lifecycle=kind)
                elif (now - dt.datetime.fromisoformat(job.queued_at)).total_seconds() >= QUEUE_ACK_SECONDS:
                    if job.status != JobStatus.FAILED:
                        job.status = JobStatus.FAILED
                        job.last_error = "queue delivery not confirmed within 120s; not resending; check the original TUI"
                        job.continuation_outcome = "unconfirmed"
                        self.store.save_job(job)
                        self._record(job, "QUEUE_UNCONFIRMED", now)
                        self._record(job, "CONTINUATION_UNCONFIRMED", now)
                    return job
                else:
                    return job
            else:
                continuation_pending = job.continuation_outcome == "queued"

            if key != job.observed_event:
                job.observed_event = key
                job.scheduled_resume = None
                job.parsed_reset = None
                job.last_error = None
                if kind == "task_started":
                    if continuation_pending:
                        self._bind_continuation(job, event, tail, now)
                        job.continuation_outcome = "started"
                    job.status = JobStatus.RUNNING
                    job.last_start = event.get("timestamp", now.isoformat())
                    job.exit_classification = None
                    self._record(job, "TURN_STARTED", now)
                    if continuation_pending:
                        self._record(job, "CONTINUATION_STARTED", now,
                                     turn_id=job.continuation_turn_id)
                        self._observe_progress(job, progress, now)
                elif kind == "turn_aborted":
                    if continuation_pending:
                        self._bind_continuation(job, event, tail, now)
                    job.status = JobStatus.CANCELLED
                    job.exit_classification = ExitClassification.USER_INTERRUPT.value
                    self._record(job, "USER_INTERRUPT", now)
                    if continuation_pending:
                        self._mark_continuation_terminal(job, "aborted", now)
                elif kind == "task_complete":
                    if continuation_pending:
                        self._bind_continuation(job, event, tail, now)
                        self._observe_progress(job, progress, now)
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
                        if continuation_pending:
                            self._mark_continuation_terminal(
                                job,
                                "rate_limited_after_progress" if job.progress_item_count else
                                "rate_limited_before_progress",
                                now,
                            )
                    elif payload.get("error"):
                        job.status = JobStatus.FAILED
                        job.exit_classification = ExitClassification.UNKNOWN_FAILURE.value
                        job.last_error = "Codex turn failed without a rate-limit error; inspect original TUI"
                        self._record(job, "TURN_FAILED", now)
                        if continuation_pending:
                            self._mark_continuation_terminal(job, "failed", now)
                    else:
                        job.status = JobStatus.COMPLETED
                        job.exit_classification = ExitClassification.NORMAL_COMPLETION.value
                        job.rate_limit_retries = 0
                        self._record(job, "TURN_COMPLETED", now)
                        if continuation_pending:
                            self._mark_continuation_terminal(job, "completed", now)
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
        job.continuation_turn_id = None
        job.continuation_started_at = None
        job.last_progress_at = None
        job.progress_item_count = 0
        job.progress_summary = []
        job.continuation_outcome = "queued"
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
            self._record(job, "QUEUED", now, queue_id=job.queue_id,
                         exit_code=result.returncode, outcome=job.continuation_outcome)
        except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
            job.status = JobStatus.FAILED
            job.last_error = f"queue outcome unconfirmed; not resending: {exc}"
            job.continuation_outcome = "unconfirmed"
            self._record(job, "QUEUE_FAILED", now, error=job.last_error)
            self._record(job, "CONTINUATION_UNCONFIRMED", now)
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
